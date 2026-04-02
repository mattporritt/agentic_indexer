<?php
namespace aiprovider_openai;

defined('MOODLE_INTERNAL') || die();

class provider extends \core_ai\provider {
    /**
     * Return OpenAI action settings.
     *
     * @return array
     */
    public function get_action_settings(): array {
        return ['provider' => 'openai'];
    }

    public function build_image_form(): form\action_generate_image_form {
        return new form\action_generate_image_form();
    }
}
