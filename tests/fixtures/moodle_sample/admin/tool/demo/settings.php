<?php
defined('MOODLE_INTERNAL') || die();

$settings->add(new admin_setting_configtext(
    'tool_demo/enabled',
    get_string('enabled', 'tool_demo'),
    get_string('enabled_desc', 'tool_demo'),
    1
));
