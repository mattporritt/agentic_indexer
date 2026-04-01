<?php
defined('MOODLE_INTERNAL') || die();

/**
 * Returns a localised string.
 *
 * @param string $identifier String identifier.
 * @param ?string $component Optional frankenstyle component.
 * @return string Localised string value.
 */
function get_string(string $identifier, ?string $component = null): string {
    return $component ? "{$component}:{$identifier}" : $identifier;
}

