<?php
namespace mod_assign\local;

defined('MOODLE_INTERNAL') || die();

/**
 * Contract for assignment views.
 */
interface viewable {
    /**
     * Render a view tab.
     *
     * @param ?string $tab Optional tab name.
     * @return string Rendered tab output.
     */
    public function view(?string $tab = null): string;
}

