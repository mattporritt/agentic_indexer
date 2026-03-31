import PluginManagementTable from 'core_admin/plugin_management_table';
import {call as fetchMany} from 'core/ajax';
import {buildActionPayload} from 'core_ai/local_actions';

/**
 * Realistic Moodle-style AI provider management table module.
 */
export default class extends PluginManagementTable {
    async loadActions(providerName) {
        const payload = buildActionPayload(providerName);
        return fetchMany([
            {
                methodname: 'core_ai_get_provider_actions',
                args: payload,
            },
        ]);
    }
}

