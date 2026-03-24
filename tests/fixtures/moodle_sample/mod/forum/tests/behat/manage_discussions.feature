Feature: Manage forum discussions
  Scenario: Teacher can open a forum discussion
    Given I log in as "teacher1"
    When I am on the "General" "forum activity" page
    Then I should see "Welcome"
