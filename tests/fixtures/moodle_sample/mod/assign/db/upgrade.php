<?php
defined('MOODLE_INTERNAL') || die();

function xmldb_assign_upgrade(int $oldversion): bool {
    global $DB;

    $DB->execute('UPDATE {assign_submission} SET status = ? WHERE status = ?', ['submitted', 'draft']);

    return true;
}
