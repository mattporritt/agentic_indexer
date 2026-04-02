<?php
namespace aiprovider_awsbedrock;

defined('MOODLE_INTERNAL') || die();

class provider extends \core_ai\provider {
    /**
     * Return AWS Bedrock action settings.
     *
     * @return array
     */
    public function get_action_settings(): array {
        return ['provider' => 'awsbedrock'];
    }
}
