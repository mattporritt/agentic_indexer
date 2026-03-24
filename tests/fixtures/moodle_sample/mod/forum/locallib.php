<?php
defined('MOODLE_INTERNAL') || die();

function forum_format_subject(string $subject): string {
    return trim($subject);
}
