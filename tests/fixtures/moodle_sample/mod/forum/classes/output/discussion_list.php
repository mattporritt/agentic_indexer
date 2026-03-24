<?php
namespace mod_forum\output;

defined('MOODLE_INTERNAL') || die();

interface renderable {}

class discussion_list implements renderable {
    public function export_for_template(\renderer_base $output): array {
        return ['title' => get_string('pluginname', 'mod_forum')];
    }
}
