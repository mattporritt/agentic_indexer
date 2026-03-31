<?php
namespace aiprovider_openai;

defined('MOODLE_INTERNAL') || die();

class provider {
    public function build_image_form(): form\action_generate_image_form {
        return new form\action_generate_image_form();
    }
}

