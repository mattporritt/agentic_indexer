// Legacy Moodle AMD module pattern still found across the codebase.
define(['jquery', 'core/ajax', 'mod_forum/repository'], function($, Ajax, Repository) {
    const init = function(root) {
        $(root).on('click', '[data-action="refresh"]', function() {
            Ajax.call([{methodname: 'mod_forum_get_forums_by_courses', args: {courseids: [1]}}]);
            Repository.load();
        });
    };

    return {
        init: init
    };
});

