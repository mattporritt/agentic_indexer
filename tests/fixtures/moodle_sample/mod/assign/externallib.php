<?php
defined('MOODLE_INTERNAL') || die();

require_once(__DIR__ . '/locallib.php');

/**
 * Submit the grading form.
 *
 * @param int $userid User identifier.
 * @return array Submission result.
 */
function submit_grading_form(int $userid): array {
    $assign = new assign();
    $rendered = $assign->view();

    return ['status' => 'ok'];
}

/**
 * Remove an assignment instance through the legacy model.
 *
 * @param object $context Runtime context.
 * @return bool
 */
function remove_assignment_instance(object $context): bool {
    $assignment = new assign($context, null, null);
    return $assignment->delete_instance();
}
