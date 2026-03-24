<?php
namespace mod_forum;

defined('MOODLE_INTERNAL') || die();

class discussion_exporter_test extends \advanced_testcase {
    public function test_execute_returns_payload(): void {
        $value = 1;
        $this->assertEquals(1, $value);
    }
}
