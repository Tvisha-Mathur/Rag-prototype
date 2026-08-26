"""Purpose: Verifies that taxonomy validation only accepts active source chunks.

Used by: Executed by pytest as part of the taxonomy regression suite.
"""

from backend.app.services.classification_validator import ClassificationValidator


class FakeCollection:
    def __init__(self, records=None):
        self.records = records or []

    def find_one(self, query, _projection):
        return next(
            (
                record
                for record in self.records
                if all(record.get(key) == value for key, value in query.items())
            ),
            None,
        )


class FakeDatabase:
    def __init__(self, taxonomy_records):
        self.collections = {
            "knowledge_chunks": FakeCollection(taxonomy_records),
            "severity_impact_rules": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_only_exact_active_taxonomy_chunk_is_approved():
    validator = ClassificationValidator(FakeDatabase([
        {
            "chunk_type": "taxonomy",
            "domain": "Guest-Related Incidents",
            "subdomain": "Guest Medical Emergency",
            "active": True,
        },
        {
            "chunk_type": "taxonomy",
            "domain": "Guest-Related Incidents",
            "subdomain": "Retired Label",
            "active": False,
        },
    ]))

    assert validator.is_approved_taxonomy_pair(
        "Guest-Related Incidents", "Guest Medical Emergency"
    )
    assert not validator.is_approved_taxonomy_pair(
        "Guest-Related Incidents", "Retired Label"
    )
    assert not validator.is_approved_taxonomy_pair(
        "Guest-Related Incidents", "Invented Label"
    )
