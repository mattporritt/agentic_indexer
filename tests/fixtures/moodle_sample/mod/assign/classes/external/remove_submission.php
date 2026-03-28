<?php
namespace mod_assign\external;

defined('MOODLE_INTERNAL') || die();

class remove_submission {
    public static function execute(): array {
        return ['removed' => true];
    }
}
