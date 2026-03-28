<?php
namespace mod_assign\external;

defined('MOODLE_INTERNAL') || die();

class start_submission {
    public static function execute(): array {
        return ['started' => true];
    }
}
