"""Regression tests: regular (played) matches must behave exactly as in v0."""

from conftest import T, add_pending_match
from models import Match, Score, db

REGULAR_OUTCOME = {'type': 'REGULAR', 'reason': None, 'scores_applicable': True}


def test_regular_match_lifecycle(client, seeded):
    mid = add_pending_match('T001', 'T002')

    response = client.post('/update-score', json={
        'match_id': mid, 'tournament_id': T, 'score': '11-5', 'final': True
    })
    assert response.status_code == 200

    match = db.session.get(Match, mid)
    assert match.outcome_type == 'REGULAR'
    assert match.is_final is True
    assert match.winner_team_id == 'T001'
    assert {s.team_id: s.score for s in Score.query.filter_by(match_id=mid).all()} == {
        'T001': 11, 'T002': 5
    }

    fixture = next(
        f for f in client.get(f'/get-match-fixtures?tournament_id={T}').get_json()['matches']
        if f['match_id'] == mid
    )
    assert fixture['outcome'] == REGULAR_OUTCOME
    assert fixture['match_result'] == '11-5'
    assert fixture['match_status']['winner_team_id'] == 'T001'


def test_regular_tie_still_has_no_winner(client, seeded):
    mid = add_pending_match('T003', 'T004')
    response = client.post('/update-score', json={
        'match_id': mid, 'tournament_id': T, 'score': '10-10', 'final': True
    })
    assert response.status_code == 200

    db.session.expire_all()
    match = db.session.get(Match, mid)
    assert match.outcome_type == 'REGULAR'
    assert match.is_final is True
    assert match.winner_team_id is None


def test_legacy_blank_outcome_is_treated_as_regular(client, seeded):
    mid = seeded[('T001', 'T002')]
    db.session.execute(
        db.text("UPDATE `match` SET outcome_type = '' WHERE id = :id"), {'id': mid}
    )
    db.session.commit()

    fixture = next(
        f for f in client.get(f'/get-match-fixtures?tournament_id={T}').get_json()['matches']
        if f['match_id'] == mid
    )
    assert fixture['outcome'] == REGULAR_OUTCOME

    detail = client.get(f'/score/match?match_id={mid}&tournament_id={T}').get_json()
    assert detail['outcome'] == REGULAR_OUTCOME
