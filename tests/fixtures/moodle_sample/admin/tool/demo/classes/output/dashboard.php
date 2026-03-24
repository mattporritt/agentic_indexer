<?php
namespace tool_demo\output;

defined('MOODLE_INTERNAL') || die();

trait dashboard {
    public function heading(): string {
        return get_string('enabled', 'tool_demo');
    }
}
