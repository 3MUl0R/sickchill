import os
import time
from urllib.parse import urljoin

from sickchill import logger, settings
from sickchill.helper.common import try_int
from sickchill.oldbeard import tvcache
from sickchill.oldbeard.bs4_parser import BS4Parser
from sickchill.oldbeard.common import cpu_presets
from sickchill.providers.nzb.NZBProvider import NZBProvider


class NewznabProvider(NZBProvider, tvcache.RSSTorrentMixin):
    """
    Generic provider for built-in and custom providers who expose a newznab
    compatible api.
    Tested with: newznab, nzedb, spotweb, torznab
    """

    # Safety bound on how many free-text (q=) requests an anime episode/season search may
    # issue per mode. Anime is searched additively (structured season/ep + structured
    # absolute + free-text variants from all_possible_show_names), and the alias list can be
    # large; this caps provider request volume. Capped drops are logged (never silent).
    ANIME_MAX_FREETEXT_VARIANTS = 8

    def __init__(self, name, url, key="0", categories="5030,5040", search_mode="episode", search_fallback=False, enable_daily=True, enable_backlog=False):
        super().__init__(name)

        self.url = url
        self.key = key

        self.search_mode = search_mode
        self.search_fallback = search_fallback
        self.enable_daily = enable_daily
        self.enable_backlog = enable_backlog

        # 0 in the key spot indicates that no key is needed
        self.needs_auth = self.key != "0"
        self.public = not self.needs_auth

        self.categories = categories if categories else "5030,5040"

        self.default = False

        self._caps = False
        self.use_tv_search = None
        self.cap_tv_search = None
        # self.cap_search = None
        # self.cap_movie_search = None
        # self.cap_audio_search = None

        self.cache = tvcache.TVCache(self, min_time=30)  # only poll newznab providers every 30 minutes max

    def config_string(self):
        """
        Generates a '|' delimited string of instance attributes, for saving to config.ini
        """
        return f"{self.name}|{self.url}|{self.key}|{self.categories}|{self.enabled:d}|{self.search_mode}|{self.search_fallback:d}|{self.enable_daily:d}|{self.enable_backlog:d}"

    @staticmethod
    def providers_list(data):
        default_list = [x for x in (NewznabProvider._make_provider(x) for x in NewznabProvider._get_default_providers().split("!!!")) if x]
        providers_list = [x for x in (NewznabProvider._make_provider(x) for x in data.split("!!!")) if x]
        seen_values = set()
        providers_set = []

        for provider in providers_list:
            value = provider.name

            if value not in seen_values:
                providers_set.append(provider)
                seen_values.add(value)

        providers_list = providers_set
        providers_dict = {x.name: x for x in providers_list}

        for default in default_list:
            if not default:
                continue

            if default.name not in providers_dict:
                providers_dict[default.name] = default

                providers_dict[default.name].default = True
                providers_dict[default.name].name = default.name
                providers_dict[default.name].url = default.url
                providers_dict[default.name].needs_auth = default.needs_auth
                providers_dict[default.name].search_mode = default.search_mode
                providers_dict[default.name].search_fallback = default.search_fallback
                providers_dict[default.name].enable_daily = default.enable_daily
                providers_dict[default.name].enable_backlog = default.enable_backlog
                providers_dict[default.name].categories = (
                    ",".join([x for x in providers_dict[default.name].categories.split(",") if 5000 <= try_int(x) <= 5999]) or default.categories
                )

        return [x for x in providers_list if x]

    def image_name(self):
        """
        Checks if we have an image for this provider already.
        Returns found image or the default newznab image
        """
        if os.path.isfile(os.path.join(settings.PROG_DIR, "gui", settings.GUI_NAME, "images", "providers", self.get_id() + ".png")):
            return self.get_id() + ".png"
        return "newznab.png"

    @property
    def caps(self):
        return self._caps

    @caps.setter
    def caps(self, data):
        # Override nzb.su - tvsearch without tvdbid, with q param
        if "nzb.su" in self.url:
            self.use_tv_search = True
            self.cap_tv_search = ""
            self._caps = True
            return

        elm = data.find("tv-search")
        self.use_tv_search = elm and elm.get("available") == "yes"
        if self.use_tv_search:
            self.cap_tv_search = elm.get("supportedParams", "tvdbid,season,ep")

        self._caps = any([self.cap_tv_search])

    @property
    def request_url(self) -> str:
        url_parts: list = self.url.lower().split("/")
        if "api" in url_parts and url_parts.index("api") > 2:
            response = self.url
        else:
            response = urljoin(self.url, "api")

        if "morethantv" in response.lower():
            response = response.rstrip("/")

        return response

    def get_newznab_categories(self, just_caps=False):
        """
        Uses the newznab provider url and apikey to get the capabilities.
        Makes use of the default newznab caps param. e.a. http://yournewznab/api?t=caps&apikey=skdfiw7823sdkdsfjsfk
        Returns a tuple with (success or not, array with dicts [{'id': '5070', 'name': 'Anime'}], error message)
        """
        return_categories = []

        if not self._check_auth():
            return False, return_categories, "Provider requires auth and your key is not set"

        url_params = {"t": "caps"}
        if self.needs_auth and self.key:
            url_params["apikey"] = self.key

        data = self.get_url(self.request_url, params=url_params, returns="text")
        if not data:
            error_string = "Error getting caps xml for [{0}]".format(self.name)
            logger.warning(error_string)
            return False, return_categories, error_string

        with BS4Parser(data, language="xml") as html:
            if not html.find("categories"):
                error_string = "Error parsing caps xml for [{0}]".format(self.name)
                logger.debug(error_string)
                return False, return_categories, error_string

            self.torznab = self.check_torznab(html)
            self.caps = html.find("searching")
            if just_caps:
                return True, return_categories, "Just checking caps!"

            for category in html("category"):
                if "TV" in category.get("name", "") and category.get("id", ""):
                    return_categories.append({"id": category["id"], "name": category["name"]})
                    for subcat in category("subcat"):
                        if subcat.get("name", "") and subcat.get("id", ""):
                            return_categories.append({"id": subcat["id"], "name": subcat["name"]})

            return True, return_categories, ""

    @staticmethod
    def _get_default_providers():
        # name|url|key|categories|enabled|search_mode|search_fallback|enable_daily|enable_backlog
        return (
            "NZB.Cat|https://nzb.cat/||5030,5040,5010|0|episode|1|1|1!!!"
            + "NZBFinder.ws|https://nzbfinder.ws/||5030,5040,5010,5045|0|episode|1|1|1!!!"
            + "NZBGeek|https://api.nzbgeek.info/||5030,5040|0|episode|0|0|0!!!"
            + "Usenet-Crawler|https://www.usenet-crawler.com/||5030,5040|0|episode|0|0|0!!!"
            + "DOGnzb|https://api.dognzb.cr/||5030,5040,5060,5070|0|episode|0|1|1"
        )

    def _check_auth(self):
        """
        Checks that user has set their api key if it is needed
        Returns: True/False
        """
        if self.needs_auth and not self.key:
            logger.warning(_("Invalid api key. Check your settings"))
            return False

        return True

    def check_auth_from_data(self, data):
        """
        Checks that the returned data is valid
        Returns: _check_auth if valid otherwise False if there is an error
        """
        if data("categories") + data("item"):
            return self._check_auth()

        try:
            err_desc = data.error.attrs["description"]
            if not err_desc:
                raise AttributeError
        except (AttributeError, TypeError):
            return self._check_auth()

        logger.info(err_desc)

        return False

    @staticmethod
    def _make_provider(config):
        if not config:
            return None

        enable_backlog = 0
        enable_daily = 0
        search_fallback = 0
        search_mode = "episode"

        try:
            values = config.split("|")

            if len(values) == 9:
                name, url, key, category_ids, enabled, search_mode, search_fallback, enable_daily, enable_backlog = values
            else:
                name = values[0]
                url = values[1]
                key = values[2]
                category_ids = values[3]
                enabled = values[4]
        except ValueError:
            logger.exception(_("Skipping Newznab provider string: '{0}', incorrect format").format(config))
            return None

        search_mode = NewznabProvider.normalize_search_mode(search_mode)

        new_provider = NewznabProvider(
            name,
            url,
            key=key,
            categories=category_ids,
            search_mode=search_mode,
            search_fallback=search_fallback,
            enable_daily=enable_daily,
            enable_backlog=enable_backlog,
        )
        new_provider.enabled = enabled == "1"

        return new_provider

    def search(self, search_strings):
        """
        Searches indexer using the params in search_strings, either for latest releases, or a string/id search
        Returns: list of results in dict form
        """
        results = []
        if not self._check_auth():
            return results

        if "gingadaddy" not in self.url:  # gingadaddy has no caps.
            if not self.caps:
                self.get_newznab_categories(just_caps=True)

            if not self.caps:
                return results

        for mode in search_strings:
            # Anime (episode/season modes): search additively in multiple ways and pool the
            # results, instead of the historical absolute-only query that frequently returned
            # nothing. Air-by-date/sports anime and RSS keep the original path below.
            if mode != "RSS" and self.show.is_anime and not (self.show.air_by_date or self.show.sports):
                results += self._search_anime_mode(mode, search_strings[mode])
                continue

            search_params = {
                "t": ("search", "tvsearch")[bool(self.use_tv_search)],
                "limit": 100,
                "offset": 0,
                "cat": self.categories.strip(", ") or "5030,5040",
                "maxage": settings.USENET_RETENTION,
            }

            if self.needs_auth and self.key:
                search_params["apikey"] = self.key

            if mode != "RSS":
                if self.use_tv_search:
                    if "tvdbid" in str(self.cap_tv_search):
                        search_params["tvdbid"] = self.show.indexerid

                    if self.show.air_by_date or self.show.sports:
                        search_params["q"] = str(self.current_episode_object.airdate)
                    elif self.show.is_anime:
                        search_params["ep"] = self.current_episode_object.absolute_number
                    else:
                        search_params["season"] = self.current_episode_object.scene_season
                        search_params["ep"] = self.current_episode_object.scene_episode

                if mode == "Season":
                    search_params.pop("ep", "")

            if self.torznab:
                search_params.pop("ep", "")
                search_params.pop("season", "")

            items = []
            logger.debug(_("Search Mode: {0}").format(mode))
            for search_string in {*search_strings[mode]}:
                if mode != "RSS":
                    logger.debug(_("Search String: {search_string}").format(search_string=search_string))

                    if "tvdbid" not in search_params:
                        search_params["q"] = search_string

                time.sleep(cpu_presets[settings.CPU_PRESET])
                data = self.get_url(self.request_url, params=search_params, returns="text")

                if not data:
                    logger.debug(_("No data was returned from the provider"))
                    break

                with BS4Parser(data, language="xml") as html:
                    if not self.check_auth_from_data(html):
                        break

                    self.torznab = self.check_torznab(html)

                    for item in html("item"):
                        try:
                            result = self.parse_feed_item(item, self.url)
                            if result:
                                items.append(result)
                        except Exception:
                            continue

                # Since we aren't using the search string,
                # break out of the search string loop
                if "tvdbid" in search_params:
                    break

            if self.torznab:
                results.sort(key=lambda d: try_int(d.get("seeders", 0)), reverse=True)
            results += items

        return results

    def _anime_base_params(self):
        """Base newznab request params shared by every anime query variant."""
        params = {
            "t": ("search", "tvsearch")[bool(self.use_tv_search)],
            "limit": 100,
            "offset": 0,
            "cat": self.categories.strip(", ") or "5030,5040",
            "maxage": settings.USENET_RETENTION,
        }
        if self.needs_auth and self.key:
            params["apikey"] = self.key
        return params

    def _build_anime_variants(self, mode, search_strings):
        """
        Build the list of (label, params) request variants for an anime episode/season search.

        Each variant is a freshly built params dict (no mutation/reuse across variants):
          * structured season/episode  (tvdbid + season + ep)      [Episode mode]
          * structured absolute number (tvdbid + ep=absolute)      [Episode mode]
          * structured season pack     (tvdbid + season)           [Season mode]
          * free-text                  (q=<string>)                [both modes, capped]

        Structured (tvdbid) variants are only emitted when the provider advertises tvdbid
        support and we are not talking to a torznab/jackett server (torznab ignores
        tvdbid/season/ep, so a structured query would degrade to a useless tvdbid-only or
        empty request). In the torznab case we rely on free-text, never a bare id query.
        """
        episode = self.current_episode_object
        use_tvdbid = bool(self.use_tv_search) and "tvdbid" in str(self.cap_tv_search) and not self.torznab

        variants = []

        if use_tvdbid:
            if mode == "Season":
                # Season pack: tvdbid + season. Never tvdbid-only (which would match the
                # whole show); when we cannot scope to a season we fall back to free-text.
                if episode.scene_season is not None:
                    season_params = self._anime_base_params()
                    season_params["tvdbid"] = self.show.indexerid
                    season_params["season"] = episode.scene_season
                    variants.append(("structured-season", season_params))
            else:
                # The normal season/episode query a regular show issues (the high-impact fix).
                sxe_params = self._anime_base_params()
                sxe_params["tvdbid"] = self.show.indexerid
                sxe_params["season"] = episode.scene_season
                sxe_params["ep"] = episode.scene_episode
                variants.append(("structured-season-ep", sxe_params))

                # Absolute-number add-on (the historical anime query), kept as a booster.
                if episode.absolute_number:
                    abs_params = self._anime_base_params()
                    abs_params["tvdbid"] = self.show.indexerid
                    abs_params["ep"] = episode.absolute_number
                    variants.append(("structured-absolute", abs_params))

        # Free-text variants from get_episode/season_search_strings (deduped, capped).
        free_text = sorted({s for s in search_strings if s})
        capped = free_text[: self.ANIME_MAX_FREETEXT_VARIANTS]
        if len(free_text) > len(capped):
            logger.debug(
                "[{0}] Capping anime free-text {1} queries from {2} to {3} (ANIME_MAX_FREETEXT_VARIANTS)".format(
                    self.name, mode, len(free_text), len(capped)
                )
            )
        for search_string in capped:
            ft_params = self._anime_base_params()
            ft_params["q"] = search_string
            variants.append(("free-text", ft_params))

        return variants

    def _search_anime_mode(self, mode, search_strings):
        """Issue the additive anime query variants for one mode and return deduped items."""
        variants = self._build_anime_variants(mode, search_strings)
        if not variants:
            return []

        items = []
        logger.debug(_("Search Mode: {0}").format(mode))
        for label, search_params in variants:
            if "q" in search_params:
                logger.debug(_("Search String: {search_string}").format(search_string=search_params["q"]))
            else:
                logger.debug("[{0}] Anime structured query: {1}".format(self.name, label))

            time.sleep(cpu_presets[settings.CPU_PRESET])
            data = self.get_url(self.request_url, params=search_params, returns="text")

            if not data:
                logger.debug(_("No data was returned from the provider"))
                break

            with BS4Parser(data, language="xml") as html:
                if not self.check_auth_from_data(html):
                    break

                self.torznab = self.check_torznab(html)

                for item in html("item"):
                    try:
                        result = self.parse_feed_item(item, self.url)
                        if result:
                            items.append(result)
                    except Exception:
                        continue

        items = self._dedupe_results(items)
        if self.torznab:
            items.sort(key=lambda d: try_int(d.get("seeders", 0)), reverse=True)
        return items

    @staticmethod
    def _dedupe_results(items):
        """
        Dedupe pooled results from multiple anime query variants.

        Primary key is the normalized download link plus the item hash (present for
        torznab/torrent results); falls back to title+size when no link is available.
        """
        seen = set()
        deduped = []
        for item in items:
            link = (item.get("link") or "").strip().lower()
            item_hash = (item.get("hash") or "").strip().lower()
            if link or item_hash:
                key = ("link", link, item_hash)
            else:
                key = ("title", (item.get("title") or "").strip().lower(), item.get("size", -1))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _get_size(self, item):
        """
        Gets size info from a result item
        Returns int size or -1
        """
        return try_int(item.get("size", -1), -1)


Provider = NewznabProvider
