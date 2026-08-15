import html
import json
import re
import traceback

import requests
import tvdbsimple
import tvdbsimple.base

import sickchill.start
from sickchill import logger, settings
from sickchill.show.indexers.base import Indexer
from sickchill.show.indexers.wrappers import ExceptionDecorator
from sickchill.tv import TVEpisode

TVDB_REQUEST_TIMEOUT = 30


class _TimeoutRequests:
    """
    Give tvdbsimple's requests a timeout.

    tvdbsimple calls ``requests.request()`` with no timeout, so a theTVDB connection that
    stalls rather than answering hangs the caller forever -- including the add show page,
    which would then never fall through to the TVmaze search below. tvdbsimple.base is the
    only module in that package that touches requests, and only ever through ``.request()``,
    so rebinding the name there injects a default without altering the requests module that
    the rest of SickChill shares.
    """

    def __init__(self, timeout):
        self.timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return requests.request(*args, **kwargs)


if not isinstance(tvdbsimple.base.requests, _TimeoutRequests):
    tvdbsimple.base.requests = _TimeoutRequests(TVDB_REQUEST_TIMEOUT)


class TVDB(Indexer):
    def __init__(self):
        super(TVDB, self).__init__()
        self.name = "theTVDB"
        self.slug = "tvdb"
        self.api_key = "6aa6e4ecae5b56e9644f6a303c0739b6"
        self.show_url = "https://thetvdb.com/?tab=series&id="
        self.base_url = "https://thetvdb.com/api/%(apikey)s/series/"
        self.icon = "images/indexers/thetvdb16.png"
        tvdbsimple.keys.API_KEY = self.api_key
        self._search = tvdbsimple.search.Search().series
        self._series = tvdbsimple.Series
        self.series_episodes = tvdbsimple.Series_Episodes
        self.series_images = tvdbsimple.Series_Images
        self.updates = tvdbsimple.Updates

    @ExceptionDecorator()
    def series(self, *args, **kwargs):
        result = self._series(*args, **kwargs)
        if result:
            result.info(language=kwargs.get("language"))
        return result

    @ExceptionDecorator()
    def get_series_by_id(self, indexerid, language=None):
        result = self._series(indexerid, language)
        if result:
            result.info(language=language)
        return result

    @ExceptionDecorator()
    def series_from_show(self, show):
        result = self._series(show.indexerid, show.lang)
        if result:
            result.info(language=show.lang)
        return result

    def series_from_episode(self, episode):
        return self.series_from_show(episode.show)

    def get_series_by_name(self, name, indexerid=None, language=None):
        if indexerid:
            return self.get_series_by_id(indexerid, language)

        # Just return the first result for now
        try:
            result = self._series(self.search(name, language)[0]["id"])
            if result:
                result.info(language=language)
            return result
        except Exception:
            pass

    @ExceptionDecorator()
    def episodes(self, show, season=None):
        if show.dvdorder:
            result = self.series_episodes(show.indexerid, dvdSeason=season, language=show.lang).all()
        else:
            result = self.series_episodes(show.indexerid, airedSeason=season, language=show.lang).all()

        return result

    @ExceptionDecorator()
    def episode(self, item, season=None, episode=None, **kwargs):
        if isinstance(item, TVEpisode):
            show = item.show
            season = item.season
            episode = item.episode
        else:
            show = item

        if show.dvdorder:
            result = self.series_episodes(show.indexerid, dvdSeason=season, dvdEpisode=episode, language=show.lang, **kwargs).all()[0]
        else:
            result = self.series_episodes(show.indexerid, airedSeason=season, airedEpisode=episode, language=show.lang, **kwargs).all()[0]

        return result

    @ExceptionDecorator(default_return=list())
    def search(self, name, language=None, exact=False, indexer_id=False):
        """
        :param name: Show name to search for
        :param language: Language of the show info we want
        :param exact: Exact when adding existing, processed when adding new shows
        :param indexer_id: Exact indexer id to get, either imdb or tvdb id.
        :return: list of series objects
        """
        language = language or self.language
        result = []
        if isinstance(name, bytes):
            name = name.decode()

        if not name:
            return result

        if re.match(r"^t?t?\d{7,8}$", name) or re.match(r"^\d{6}$", name):
            try:
                if re.match(r"^t?t?\d{7,8}$", name):
                    result = self._search(imdbId=f"tt{name.strip('t')}", language=language)
                elif re.match(r"^\d{6}$", name):
                    series = self._series(name, language=language)
                    if series:
                        result = [series.info(language)]
            except Exception:
                logger.debug(traceback.format_exc())
            return result or []

        # ----- name search path -----
        # Name as provided (usually from nfo)
        names = [name]
        if not exact:
            # Name without year and separator
            test = re.match(r"^(.+?)[. _-]+\(\d{4}\)?$", name)
            if test:
                names.append(test.group(1).strip())
            # Name with spaces
            if re.search(r"[. _-]", name):
                names.append(re.sub(r"[. _-]", " ", name).strip())
                if test:
                    # Name with spaces and without year
                    names.append(re.sub(r"[. _-]", " ", test.group(1)).strip())

        seen_ids = set()

        # 1. Try theTVDB first. dict.fromkeys de-duplicates while keeping the order,
        # so the name as provided is always attempted before the derived variants.
        for attempt in dict.fromkeys(n for n in names if n.strip()):
            try:
                tvdb_results = self._search(attempt, language=language) or []
                for item in tvdb_results:
                    sid = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                    if sid and sid not in seen_ids:
                        result.append(item)
                        seen_ids.add(sid)
                if result:
                    break
            except requests.exceptions.HTTPError as error:
                # theTVDB retired name search on the v2 api: it answers 404 for every
                # name query now, so this is the normal path rather than an error.
                if getattr(error, "response", None) is not None and error.response.status_code == 404:
                    logger.debug(f"theTVDB name search 404 for '{attempt}' (trying TVmaze)")
                else:
                    logger.debug(traceback.format_exc())
            except Exception:
                logger.debug(traceback.format_exc())

        # 2. Supplement with TVmaze only when theTVDB gave nothing. TVmaze is used purely
        # to turn a name into a thetvdb id; the series details still come from theTVDB,
        # whose by-id endpoints are unaffected.
        if not result:
            try:
                from . import tvmaze

                for attempt in dict.fromkeys(n for n in names if n.strip()):
                    for show in tvmaze.search(attempt):
                        tvdb_id = show.get("externals", {}).get("thetvdb")
                        if not tvdb_id or tvdb_id in seen_ids:
                            continue
                        # Claim the id before fetching, not after: a detail lookup that
                        # fails or comes back untitled must not be retried once per name
                        # variant, which during an outage is the slowest possible way to
                        # arrive at the same empty result.
                        seen_ids.add(tvdb_id)
                        try:
                            series = self._series(tvdb_id, language=language)
                            if not series:
                                continue
                            info = series.info(language)
                            # theTVDB can hold an entry with no title in the requested
                            # language. Callers treat seriesName as a string (the exact
                            # match filter lowercases it), so drop those rather than
                            # hand back a row that blows up downstream.
                            if not info or not info.get("seriesName"):
                                continue
                            result.append(info)
                        except Exception:
                            logger.debug(traceback.format_exc())
                    if result:
                        break
            except Exception:
                logger.debug(traceback.format_exc())

        return result or []

    @property
    def languages(self):
        return ["cs", "da", "de", "el", "en", "es", "fi", "fr", "he", "hr", "hu", "it", "ja", "ko", "nl", "no", "pl", "pt", "ru", "sl", "sv", "tr", "zh"]

    @property
    def lang_dict(self):
        return {
            "el": 20,
            "en": 7,
            "zh": 27,
            "it": 15,
            "cs": 28,
            "es": 16,
            "ru": 22,
            "nl": 13,
            "pt": 26,
            "no": 9,
            "tr": 21,
            "pl": 18,
            "fr": 17,
            "hr": 31,
            "de": 14,
            "da": 10,
            "fi": 11,
            "hu": 19,
            "ja": 25,
            "he": 24,
            "ko": 32,
            "sv": 8,
            "sl": 30,
        }

    @staticmethod
    def complete_image_url(location):
        location = location.strip()
        if not location:
            return location
        return f"https://artworks.thetvdb.com/banners/{re.sub(r'^_cache/', '', location)}"

    @ExceptionDecorator(default_return="", catch=(requests.exceptions.RequestException, requests.exceptions.HTTPError, KeyError, Exception), image_api=True)
    def __call_images_api(self, show, thumb, keyType, subKey=None, lang=None, multiple=False):
        api_results = self.series_images(show.indexerid, lang or show.lang, keyType=keyType, subKey=subKey)
        try:
            images = getattr(api_results, keyType)(lang or show.lang)
        except:
            return [] if multiple else ""

        images = sorted(images, key=lambda img: img["ratingsInfo"]["average"], reverse=True)
        return [self.complete_image_url(image["fileName"]) for image in images] if multiple else self.complete_image_url(images[0]["fileName"])

    @staticmethod
    @ExceptionDecorator()
    def actors(series):
        if hasattr(series, "actors") and callable(series.actors):
            series.actors(series.language)
        return series.actors

    def series_poster_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "poster", multiple=multiple)

    def series_banner_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "series", multiple=multiple)

    def series_fanart_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "fanart", multiple=multiple)

    def season_poster_url(self, show, season, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "season", season, multiple=multiple)

    def season_banner_url(self, show, season, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "seasonwide", season, multiple=multiple)

    @ExceptionDecorator(default_return="", catch=(requests.exceptions.RequestException, KeyError, TypeError))
    def episode_image_url(self, episode):
        return self.complete_image_url(self.episode(episode)["filename"])

    def episode_guide_url(self, show):
        # https://forum.kodi.tv/showthread.php?tid=323588
        data = html.escape(json.dumps({"apikey": self.api_key, "id": show.indexerid})).replace(" ", "")
        return tvdbsimple.base.TVDB(key=self.api_key)._get_complete_url("login") + "?" + data + "|Content-Type=application/json"

    def get_favorites(self):
        results = []
        if not (settings.TVDB_USER and settings.TVDB_USER_KEY):
            return results

        user = tvdbsimple.User(settings.TVDB_USER, settings.TVDB_USER_KEY)
        for tvdbid in user.favorites():
            results.append(self.get_series_by_id(tvdbid))

        return results

    @staticmethod
    def test_user_key(user, key):
        user_object = tvdbsimple.User(user, key)
        try:
            user_object.info()
        except Exception:
            logger.exception(traceback.format_exc())
            return False

        settings.TVDB_USER = user
        settings.TVDB_USER_KEY = key

        sickchill.start.save_config()

        return True
