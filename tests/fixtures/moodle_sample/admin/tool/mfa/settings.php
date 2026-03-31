<?php
defined('MOODLE_INTERNAL') || die();

$settings = new admin_settingpage('tool_mfa', get_string('pluginname', 'tool_mfa'));
$settings->add(new admin_setting_configcheckbox('tool_mfa/enabled', 'Enabled', 'Enable MFA', 1));

