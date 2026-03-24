<?php
defined('MOODLE_INTERNAL') || die();

require_once(__DIR__ . '/locallib.php');

function forum_user_can_view_discussion(\stdClass $context): bool {
    require_capability('mod/forum:viewdiscussion', $context);
    return true;
}
