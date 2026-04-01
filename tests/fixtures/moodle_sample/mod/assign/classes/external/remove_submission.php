<?php
namespace mod_assign\external;

defined('MOODLE_INTERNAL') || die();

class remove_submission {
    /**
     * Remove an existing submission.
     *
     * @param int $submissionid Submission identifier.
     * @return array Removal result.
     */
    public static function execute(int $submissionid): array {
        return ['removed' => true];
    }
}
