<?php
namespace mod_assign\local;

defined('MOODLE_INTERNAL') || die();

/**
 * Simple concrete implementation of the viewable contract.
 */
class simple_view implements viewable {
    /**
     * Render a simple tab.
     *
     * @param ?string $tab Optional tab name.
     * @return string Rendered view output.
     */
    public function view(?string $tab = null): string {
        return (string)($tab ?? 'simple');
    }
}
