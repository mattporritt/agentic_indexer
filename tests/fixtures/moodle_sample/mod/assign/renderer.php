<?php
defined('MOODLE_INTERNAL') || die();

require_once(__DIR__ . '/locallib.php');

function assign_render(assign $assign): string {
    return $assign->view('grading');
}

