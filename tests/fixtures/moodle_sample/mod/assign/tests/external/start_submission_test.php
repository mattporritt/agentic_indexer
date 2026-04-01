<?php
defined('MOODLE_INTERNAL') || die();

use mod_assign\external\start_submission;

class start_submission_test extends advanced_testcase {
    public function test_execute(): void {
        start_submission::execute(1, false);
        $this->assertTrue(true);
    }
}
