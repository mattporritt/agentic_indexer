<?php
namespace core_ai;

defined('MOODLE_INTERNAL') || die();

/**
 * Base AI provider fixture used for inheritance tests.
 */
abstract class provider {
    /**
     * Return provider action settings.
     *
     * @return array
     */
    public function get_action_settings(): array {
        return ['base' => true];
    }
}
