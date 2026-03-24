<?php
defined('MOODLE_INTERNAL') || die();

$capabilities = [
    'mod/forum:viewdiscussion' => [
        'captype' => 'read',
        'contextlevel' => CONTEXT_MODULE,
        'archetypes' => [
            'student' => CAP_ALLOW,
            'editingteacher' => CAP_ALLOW,
        ],
        'riskbitmask' => RISK_SPAM,
    ],
];
