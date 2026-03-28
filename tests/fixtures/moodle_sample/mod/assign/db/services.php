<?php
defined('MOODLE_INTERNAL') || die();

$functions = [
    'mod_assign_submit_grading_form' => [
        'classpath' => 'mod/assign/externallib.php',
        'methodname' => 'submit_grading_form',
    ],
    'mod_assign_remove_submission' => [
        'classname' => 'mod_assign\\external\\remove_submission',
        'methodname' => 'execute',
    ],
    'mod_assign_start_submission' => [
        'classname' => 'mod_assign\\external\\start_submission',
        'methodname' => 'execute',
    ],
];
