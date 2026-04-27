<?php
defined('MOODLE_INTERNAL') || die();

$capabilities = [
    'editor/tiny/plugins/premium:usemarkdown' => [
        'riskbitmask' => RISK_XSS,
        'captype' => 'write',
        'contextlevel' => CONTEXT_SYSTEM,
    ],
];
