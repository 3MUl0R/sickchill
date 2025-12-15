<%inherit file="/layouts/config.mako" />
<%!
    import json
    from sickchill import settings
    from sickchill.oldbeard.filters import hide
    from sickchill.oldbeard.ai.anthropic_client import AnthropicClient
%>

<%block name="tabs">
    <li><a href="#api-settings">${_('API Settings')}</a></li>
    <li><a href="#search-settings">${_('Search AI')}</a></li>
    <li><a href="#postprocess-settings">${_('Post-Processing AI')}</a></li>
    <li><a href="#usage-stats">${_('Usage & Costs')}</a></li>
</%block>

<%block name="pages">
    <form id="configForm" action="saveAI" method="post">

        <div id="config-components">

            <!-- API Settings -->
            <div id="api-settings" class="component-group">
                <div class="row">
                    <div class="col-lg-3 col-md-4 col-sm-4 col-xs-12">
                        <div class="component-group-desc">
                            <h3>${_('AI Configuration')}</h3>
                            <p>${_('Configure your Anthropic Claude API settings. AI features require a valid API key from')} <a href="https://console.anthropic.com/" target="_blank" rel="noreferrer noopener">Anthropic</a>.</p>
                            <p><b>${_('Note:')}</b> ${_('AI features are optional and only used as a fallback when rule-based logic fails.')}</p>
                        </div>
                    </div>
                    <div class="col-lg-9 col-md-8 col-sm-8 col-xs-12">
                        <fieldset class="component-group-list">

                            <div class="field-pair row">
                                <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                    <label class="component-title">${_('Enable AI Features')}</label>
                                </div>
                                <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                    <input type="checkbox" name="ai_enabled" id="ai_enabled"
                                           class="enabler" ${checked(settings.AI_ENABLED)}/>
                                    <label for="ai_enabled">${_('enable AI-powered analysis for search and post-processing fallback')}</label>
                                </div>
                            </div>

                            <div id="content_ai_enabled" ${hidden(settings.AI_ENABLED)}>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Anthropic API Key')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <div class="row">
                                            <div class="col-md-12">
                                                <input type="password" name="anthropic_api_key" id="anthropic_api_key"
                                                       value="${hide(settings.ANTHROPIC_API_KEY)}"
                                                       class="form-control input-sm input350" autocapitalize="off"/>
                                            </div>
                                        </div>
                                        <div class="row">
                                            <div class="col-md-12">
                                                <label for="anthropic_api_key">${_('your Anthropic API key from')} <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer noopener">console.anthropic.com</a></label>
                                            </div>
                                        </div>
                                        <div class="row">
                                            <div class="col-md-12">
                                                <input type="button" class="btn" value="${_('Test API Key')}" id="testAnthropicKey"/>
                                                <span id="testAnthropicKey-result"></span>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('AI Model')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <select id="anthropic_model" name="anthropic_model" class="form-control input-sm input250">
                                            % for model_id, model_name in AnthropicClient.SUPPORTED_MODELS.items():
                                                <option value="${model_id}" ${selected(settings.ANTHROPIC_MODEL == model_id)}>${model_name}</option>
                                            % endfor
                                        </select>
                                        <label for="anthropic_model">${_('Claude Sonnet 4 is recommended for best accuracy')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Request Timeout')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="5" max="120" step="5" name="ai_request_timeout"
                                               id="ai_request_timeout" value="${settings.AI_REQUEST_TIMEOUT}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_request_timeout">${_('seconds to wait for AI response (default: 30)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Confidence Threshold')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="0.5" max="1.0" step="0.05" name="ai_confidence_threshold"
                                               id="ai_confidence_threshold" value="${settings.AI_CONFIDENCE_THRESHOLD}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_confidence_threshold">${_('minimum AI confidence to accept a result (0.0-1.0, default: 0.80)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Notify on AI Failure')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_notify_on_fallback_failure" id="ai_notify_on_fallback_failure"
                                               ${checked(settings.AI_NOTIFY_ON_FALLBACK_FAILURE)}/>
                                        <label for="ai_notify_on_fallback_failure">${_('send notification when AI fallback fails to find a result')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Max Calls Per Hour')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="100" step="1" name="ai_max_calls_per_hour"
                                               id="ai_max_calls_per_hour" value="${settings.AI_MAX_CALLS_PER_HOUR}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_max_calls_per_hour">${_('maximum AI API calls per hour (default: 20)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Max Calls Per Day')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="1000" step="10" name="ai_max_calls_per_day"
                                               id="ai_max_calls_per_day" value="${settings.AI_MAX_CALLS_PER_DAY}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_max_calls_per_day">${_('maximum AI API calls per day (default: 200)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Cache TTL')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="90" step="1" name="ai_cache_ttl_days"
                                               id="ai_cache_ttl_days" value="${settings.AI_CACHE_TTL_DAYS}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_cache_ttl_days">${_('days to cache AI responses (default: 30)')}</label>
                                    </div>
                                </div>

                            </div>
                        </fieldset>
                    </div>
                </div>
            </div>

            <!-- Search AI Settings -->
            <div id="search-settings" class="component-group">
                <div class="row">
                    <div class="col-lg-3 col-md-4 col-sm-4 col-xs-12">
                        <div class="component-group-desc">
                            <h3>${_('Search AI')}</h3>
                            <p>${_('AI-powered search result selection. Used as a fallback when rule-based selection fails to find a suitable download.')}</p>
                        </div>
                    </div>
                    <div class="col-lg-9 col-md-8 col-sm-8 col-xs-12">
                        <fieldset class="component-group-list">

                            <div class="field-pair row">
                                <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                    <label class="component-title">${_('Enable Search AI')}</label>
                                </div>
                                <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                    <input type="checkbox" name="ai_search_enabled" id="ai_search_enabled"
                                           class="enabler" ${checked(settings.AI_SEARCH_ENABLED)}/>
                                    <label for="ai_search_enabled">${_('use AI to select search results when rule-based picker fails')}</label>
                                </div>
                            </div>

                            <div id="content_ai_search_enabled" ${hidden(settings.AI_SEARCH_ENABLED)}>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Only on Failure')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_search_only_on_failure" id="ai_search_only_on_failure"
                                               ${checked(settings.AI_SEARCH_ONLY_ON_FAILURE)}/>
                                        <label for="ai_search_only_on_failure">${_('only use AI when rule-based selection fails (recommended)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Cooldown Per Show')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="30" step="1" name="ai_search_cooldown_days_per_show"
                                               id="ai_search_cooldown_days_per_show" value="${settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_search_cooldown_days_per_show">${_('days between AI search attempts per show (default: 7)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Minimum Results')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="20" step="1" name="ai_search_min_results"
                                               id="ai_search_min_results" value="${settings.AI_SEARCH_MIN_RESULTS}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_search_min_results">${_('minimum search results needed to trigger AI analysis')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Fallback to Rules on Error')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_search_fallback_to_rules_on_error" id="ai_search_fallback_to_rules_on_error"
                                               ${checked(settings.AI_SEARCH_FALLBACK_TO_RULES_ON_ERROR)}/>
                                        <label for="ai_search_fallback_to_rules_on_error">${_('if AI fails, continue with no result rather than blocking')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Allow Relaxed Filters')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_search_allow_relax_filters" id="ai_search_allow_relax_filters"
                                               ${checked(settings.AI_SEARCH_ALLOW_RELAX_FILTERS)}/>
                                        <label for="ai_search_allow_relax_filters">${_('allow AI to select results that failed soft filters (require/prefer words)')}</label>
                                    </div>
                                </div>

                            </div>
                        </fieldset>
                    </div>
                </div>
            </div>

            <!-- Post-Processing AI Settings -->
            <div id="postprocess-settings" class="component-group">
                <div class="row">
                    <div class="col-lg-3 col-md-4 col-sm-4 col-xs-12">
                        <div class="component-group-desc">
                            <h3>${_('Post-Processing AI')}</h3>
                            <p>${_('AI-powered file identification and analysis. Used as a fallback when parsing fails to identify a file.')}</p>
                        </div>
                    </div>
                    <div class="col-lg-9 col-md-8 col-sm-8 col-xs-12">
                        <fieldset class="component-group-list">

                            <div class="field-pair row">
                                <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                    <label class="component-title">${_('Enable File Matching AI')}</label>
                                </div>
                                <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                    <input type="checkbox" name="ai_postprocess_match_enabled" id="ai_postprocess_match_enabled"
                                           class="enabler" ${checked(settings.AI_POSTPROCESS_MATCH_ENABLED)}/>
                                    <label for="ai_postprocess_match_enabled">${_('use AI to identify files when parsing fails')}</label>
                                </div>
                            </div>

                            <div id="content_ai_postprocess_match_enabled" ${hidden(settings.AI_POSTPROCESS_MATCH_ENABLED)}>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Only on Failure')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_postprocess_match_only_on_failure" id="ai_postprocess_match_only_on_failure"
                                               ${checked(settings.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE)}/>
                                        <label for="ai_postprocess_match_only_on_failure">${_('only use AI when standard parsing fails (recommended)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Cooldown Per File')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="1" max="168" step="1" name="ai_postprocess_match_cooldown_hours_per_file"
                                               id="ai_postprocess_match_cooldown_hours_per_file" value="${settings.AI_POSTPROCESS_MATCH_COOLDOWN_HOURS_PER_FILE}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_postprocess_match_cooldown_hours_per_file">${_('hours between AI matching attempts per file (default: 72)')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Match Confidence')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="number" min="0.5" max="1.0" step="0.05" name="ai_postprocess_match_min_confidence"
                                               id="ai_postprocess_match_min_confidence" value="${settings.AI_POSTPROCESS_MATCH_MIN_CONFIDENCE}"
                                               class="form-control input-sm input75"/>
                                        <label for="ai_postprocess_match_min_confidence">${_('minimum confidence for file matching (0.0-1.0, default: 0.85)')}</label>
                                    </div>
                                </div>

                            </div>

                            <div class="field-pair row">
                                <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                    <label class="component-title">${_('Enable File Analysis AI')}</label>
                                </div>
                                <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                    <input type="checkbox" name="ai_postprocess_analyze_enabled" id="ai_postprocess_analyze_enabled"
                                           class="enabler" ${checked(settings.AI_POSTPROCESS_ANALYZE_ENABLED)}/>
                                    <label for="ai_postprocess_analyze_enabled">${_('use AI to analyze file quality and detect issues (optional)')}</label>
                                </div>
                            </div>

                            <div id="content_ai_postprocess_analyze_enabled" ${hidden(settings.AI_POSTPROCESS_ANALYZE_ENABLED)}>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Verify Quality')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_postprocess_verify_quality" id="ai_postprocess_verify_quality"
                                               ${checked(settings.AI_POSTPROCESS_VERIFY_QUALITY)}/>
                                        <label for="ai_postprocess_verify_quality">${_('verify that file quality matches the claimed quality')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Detect Issues')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_postprocess_detect_issues" id="ai_postprocess_detect_issues"
                                               ${checked(settings.AI_POSTPROCESS_DETECT_ISSUES)}/>
                                        <label for="ai_postprocess_detect_issues">${_('detect potential issues like wrong duration, file size, or codecs')}</label>
                                    </div>
                                </div>

                                <div class="field-pair row">
                                    <div class="col-lg-3 col-md-4 col-sm-5 col-xs-12">
                                        <label class="component-title">${_('Suggest Metadata')}</label>
                                    </div>
                                    <div class="col-lg-9 col-md-8 col-sm-7 col-xs-12 component-desc">
                                        <input type="checkbox" name="ai_postprocess_suggest_metadata" id="ai_postprocess_suggest_metadata"
                                               ${checked(settings.AI_POSTPROCESS_SUGGEST_METADATA)}/>
                                        <label for="ai_postprocess_suggest_metadata">${_('suggest metadata improvements for ambiguous files')}</label>
                                    </div>
                                </div>

                            </div>
                        </fieldset>
                    </div>
                </div>
            </div>

            <!-- Usage Statistics -->
            <div id="usage-stats" class="component-group">
                <div class="row">
                    <div class="col-lg-3 col-md-4 col-sm-4 col-xs-12">
                        <div class="component-group-desc">
                            <h3>${_('Usage & Costs')}</h3>
                            <p>${_('Monitor your AI API usage and estimated costs. Costs are estimates based on Anthropic pricing.')}</p>
                        </div>
                    </div>
                    <div class="col-lg-9 col-md-8 col-sm-8 col-xs-12">
                        <fieldset class="component-group-list">

                            % if usage_stats:
                            <div class="field-pair row">
                                <div class="col-md-12">
                                    <h4>${_('Usage Summary')}</h4>
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>${_('Period')}</th>
                                                <th>${_('Requests')}</th>
                                                <th>${_('Input Tokens')}</th>
                                                <th>${_('Output Tokens')}</th>
                                                <th>${_('Est. Cost')}</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <tr>
                                                <td>${_('Last 24 Hours')}</td>
                                                <td>${usage_stats['daily'].total_requests}</td>
                                                <td>${usage_stats['daily'].total_input_tokens:,}</td>
                                                <td>${usage_stats['daily'].total_output_tokens:,}</td>
                                                <td>$${'{:.4f}'.format(usage_stats['daily'].total_estimated_cost_usd)}</td>
                                            </tr>
                                            <tr>
                                                <td>${_('Last 7 Days')}</td>
                                                <td>${usage_stats['weekly'].total_requests}</td>
                                                <td>${usage_stats['weekly'].total_input_tokens:,}</td>
                                                <td>${usage_stats['weekly'].total_output_tokens:,}</td>
                                                <td>$${'{:.4f}'.format(usage_stats['weekly'].total_estimated_cost_usd)}</td>
                                            </tr>
                                            <tr>
                                                <td>${_('Last 30 Days')}</td>
                                                <td>${usage_stats['monthly'].total_requests}</td>
                                                <td>${usage_stats['monthly'].total_input_tokens:,}</td>
                                                <td>${usage_stats['monthly'].total_output_tokens:,}</td>
                                                <td>$${'{:.4f}'.format(usage_stats['monthly'].total_estimated_cost_usd)}</td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>

                            % if usage_stats['recent']:
                            <div class="field-pair row">
                                <div class="col-md-12">
                                    <h4>${_('Recent API Calls')}</h4>
                                    <table class="table table-striped table-condensed">
                                        <thead>
                                            <tr>
                                                <th>${_('Time')}</th>
                                                <th>${_('Context')}</th>
                                                <th>${_('Model')}</th>
                                                <th>${_('Tokens')}</th>
                                                <th>${_('Cost')}</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            % for record in usage_stats['recent']:
                                            <tr>
                                                <td>${'{:.0f}'.format((import_time() - record.timestamp) / 60)} min ago</td>
                                                <td>${record.context}</td>
                                                <td>${record.model.split('-')[1] if '-' in record.model else record.model}</td>
                                                <td>${record.input_tokens + record.output_tokens}</td>
                                                <td>$${'{:.4f}'.format(record.estimated_cost_usd)}</td>
                                            </tr>
                                            % endfor
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                            % endif

                            % else:
                            <div class="field-pair row">
                                <div class="col-md-12">
                                    <p class="text-muted">${_('No usage data available. Enable AI features and make some API calls to see usage statistics.')}</p>
                                </div>
                            </div>
                            % endif

                            <div class="field-pair row">
                                <div class="col-md-12">
                                    <p class="help-block">
                                        <b>${_('Pricing Reference (approximate):')}</b><br/>
                                        Claude Sonnet 4: $3/M input, $15/M output tokens<br/>
                                        Claude 3.5 Haiku: $0.25/M input, $1.25/M output tokens
                                    </p>
                                </div>
                            </div>

                        </fieldset>
                    </div>
                </div>
            </div>

            <!-- Save Button -->
            <div class="row">
                <div class="col-md-12">
                    <input type="submit" class="btn config_submitter" value="${_('Save Changes')}" />
                </div>
            </div>

        </div>
    </form>
</%block>

<%block name="scripts">
<script type="text/javascript">
$(document).ready(function() {
    // Test API key button
    $('#testAnthropicKey').click(function() {
        var btn = $(this);
        var resultSpan = $('#testAnthropicKey-result');
        var apiKey = $('#anthropic_api_key').val();
        var model = $('#anthropic_model').val();

        btn.prop('disabled', true);
        resultSpan.html('<i class="fa fa-spinner fa-spin"></i> ' + ${json.dumps(_("Testing...")) | n});

        $.post('${scRoot}/config/ai/testAnthropicKey', {
            api_key: apiKey,
            model: model
        }).done(function(data) {
            // Use safe DOM methods to prevent XSS
            var isSuccess = data.startsWith('Success');
            var span = $('<span></span>').addClass(isSuccess ? 'text-success' : 'text-danger');
            span.append($('<i></i>').addClass('fa fa-' + (isSuccess ? 'check' : 'times')));
            span.append(' ');
            span.append(document.createTextNode(data));
            resultSpan.empty().append(span);
        }).fail(function() {
            resultSpan.html('<span class="text-danger"><i class="fa fa-times"></i> ' + ${json.dumps(_("Connection error")) | n} + '</span>');
        }).always(function() {
            btn.prop('disabled', false);
        });
    });
});
</script>
</%block>

<%def name="import_time()">
<%
    import time
    return time.time()
%>
</%def>
