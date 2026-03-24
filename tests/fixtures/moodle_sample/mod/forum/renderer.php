<?php
namespace mod_forum\output;

defined('MOODLE_INTERNAL') || die();

class renderer extends \plugin_renderer_base {
    public function render_discussion_list(renderable $widget): string {
        return get_string('pluginname', 'mod_forum');
    }
}
