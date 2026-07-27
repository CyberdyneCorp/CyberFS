## ADDED Requirements

### Requirement: Tags and metadata are outside the encryption boundary

Node tags and key/value metadata SHALL be stored in plaintext, because they are indexed so they can be searched. This is a recorded consequence of the feature, not an oversight.

#### Scenario: Labels are readable from the database

- **WHEN** tags or metadata are stored
- **THEN** they SHALL be stored in plaintext and SHALL be indexable, and the documentation SHALL state that anything placed in them is readable by whoever can read the database

#### Scenario: Content encryption is unaffected

- **WHEN** a node carries tags or metadata
- **THEN** its content SHALL remain encrypted on the same terms as any other node, and no tag or metadata value SHALL be derived from the content
