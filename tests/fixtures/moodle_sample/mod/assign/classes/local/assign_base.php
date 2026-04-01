<?php
namespace mod_assign\local;

defined('MOODLE_INTERNAL') || die();

/**
 * Base class for assignment instances.
 */
abstract class assign_base {
    /**
     * Render the current assignment view.
     *
     * @param ?string $tab Optional tab name.
     * @return string Rendered output.
     */
    public function view(?string $tab = null): string {
        return (string)($tab ?? 'overview');
    }
}

