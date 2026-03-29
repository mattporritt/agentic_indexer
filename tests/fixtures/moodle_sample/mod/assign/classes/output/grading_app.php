<?php
namespace mod_assign\output;

defined('MOODLE_INTERNAL') || die();

class grading_app implements \renderable {
    public function export_for_template(\renderer_base $output): array {
        return [
            'templatename' => 'mod_assign/grading_app',
        ];
    }
}
