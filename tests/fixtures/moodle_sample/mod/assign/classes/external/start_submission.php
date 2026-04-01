<?php
namespace mod_assign\external;

defined('MOODLE_INTERNAL') || die();

class start_submission extends \external_api {
    /**
     * Start a submission attempt.
     *
     * @param int $assignmentid Assignment identifier.
     * @param bool $draft Whether to keep the attempt as a draft.
     * @return array Submission state.
     */
    public static function execute(int $assignmentid, bool $draft = false): array {
        return ['started' => true];
    }
}
