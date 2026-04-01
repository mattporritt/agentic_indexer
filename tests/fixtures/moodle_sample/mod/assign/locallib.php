<?php
defined('MOODLE_INTERNAL') || die();

use mod_assign\local\assign_base;
use mod_assign\local\viewable;

require_once(__DIR__ . '/classes/local/assign_base.php');
require_once(__DIR__ . '/classes/local/viewable.php');

/**
 * Legacy assignment model class.
 */
class assign extends assign_base implements viewable {
    /**
     * Render the current assignment view.
     *
     * @param ?string $tab Optional tab name.
     * @return string Rendered assignment output.
     */
    public function view(?string $tab = null): string {
        return get_string('pluginname', 'mod_assign') . ':' . ($tab ?? 'overview');
    }

    /**
     * Render a final status string.
     *
     * @return string Final status label.
     */
    public final function render_status(): string {
        return 'ready';
    }
}

/**
 * Build the grading app output object.
 *
 * @return \mod_assign\output\grading_app
 */
function assign_build_grading_app(): \mod_assign\output\grading_app {
    return new \mod_assign\output\grading_app();
}
