<?php
defined('MOODLE_INTERNAL') || die();

function assign_build_grading_app(): \mod_assign\output\grading_app {
    return new \mod_assign\output\grading_app();
}
