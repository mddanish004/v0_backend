"""API tests for POST /declare-walkover/<match_id> and its effect on other endpoints."""

import csv
import io

import pytest

from conftest import T, add_pending_match, create_knockout
from models import Match, Score, db
from routes.score import score_core


def declare(client, match_id, **body):
    payload = {'tournament_id': T, 'winner_team_id': 'T004'}
    payload.update(body)
    return client.post(f'/declare-walkover/{match_id}', json=payload)


def standings_row(client, team_id, pool='A', round_id='1'):
    data = client.get(f'/standings/{T}').get_json()
    return next(
        r for r in data[str(round_id)]['pools'][pool] if r['team_id'] == team_id
    )


class TestHappyPath:
    def test_declare_walkover(self, client, seeded, emitted):
        mid = seeded[('T002', 'T004')]
        response = declare(client, mid)

        assert response.status_code == 200
        body = response.get_json()
        assert body['message'] == 'Walkover declared successfully'
        match = body['match']
        assert match['outcome']['type'] == 'WALKOVER'
        assert match['outcome']['reason'] is None
        assert match['outcome']['scores_applicable'] is False
        assert match['winner_team_id'] == 'T004'
        assert match['is_final'] is True
        assert match['status'] == 'completed'

        # A walkover records no points.
        assert {s.team_id: s.score for s in Score.query.filter_by(match_id=mid).all()} == {
            'T002': 0, 'T004': 0
        }

        assert len(emitted) == 1
        args, kwargs = emitted[0]
        assert args[0] == 'score_update'
        assert kwargs['namespace'] == '/scores'

    def test_match_endpoints_expose_outcome(self, client, seeded):
        mid = add_pending_match('T001', 'T004')
        assert declare(client, mid).status_code == 200

        fixtures = client.get(f'/get-match-fixtures?tournament_id={T}').get_json()['matches']
        fixture = next(f for f in fixtures if f['match_id'] == mid)
        assert fixture['outcome']['type'] == 'WALKOVER'
        assert fixture['match_status']['winner_team_id'] == 'T004'
        assert fixture['match_status']['is_final'] is True
        assert fixture['match_status']['status'] == 'completed'

        other = next(f for f in fixtures if f['match_id'] != mid)
        assert other['outcome'] == {
            'type': 'REGULAR', 'reason': None, 'scores_applicable': True
        }

        detail = client.get(f'/score/match?match_id={mid}&tournament_id={T}').get_json()
        assert detail['outcome']['type'] == 'WALKOVER'
        assert detail['winner_team_id'] == 'T004'

        match = db.session.get(Match, mid)
        match.court_number = 1
        db.session.commit()

        court = client.get(f'/tournaments/{T}/court-matches?court_number=1').get_json()['matches']
        court_match = next(m for m in court if m['match_id'] == mid)
        assert court_match['outcome']['type'] == 'WALKOVER'
        assert court_match['winner_team_id'] == 'T004'

    def test_repeated_request_is_idempotent(self, client, seeded, emitted):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        again = declare(client, mid)
        assert again.status_code == 200
        assert again.get_json()['message'] == 'Walkover already recorded'
        assert len(emitted) == 1  # Only the first declaration is announced.

    def test_winner_advances_in_knockout(self, client, seeded):
        bracket = create_knockout(client, [('T001', 'T005'), ('T003', 'T007')])
        sf1 = bracket['SF1']['id']
        sf2 = bracket['SF2']['id']
        final = bracket['F1']['id']

        assert declare(client, sf1, winner_team_id='T005').status_code == 200
        final_match = db.session.get(Match, final)
        assert 'T005' in (final_match.team1_id, final_match.team2_id)

        assert declare(client, sf2, winner_team_id='T007').status_code == 200
        db.session.expire_all()
        final_match = db.session.get(Match, final)
        assert {final_match.team1_id, final_match.team2_id} == {'T005', 'T007'}

    def test_walkover_on_final_without_successor(self, client, seeded):
        bracket = create_knockout(client, [('T001', 'T005'), ('T003', 'T007')])
        sf1 = bracket['SF1']['id']
        sf2 = bracket['SF2']['id']
        final = bracket['F1']['id']

        assert declare(client, sf1, winner_team_id='T005').status_code == 200
        assert declare(client, sf2, winner_team_id='T007').status_code == 200
        response = declare(client, final, winner_team_id='T007')
        assert response.status_code == 200
        assert response.get_json()['match']['successor'] is None


class TestValidation:
    @pytest.mark.parametrize('body,error', [
        ({'winner_team_id': None}, 'tournament_id and winner_team_id are required'),
        ({'winner_team_id': 5}, 'winner_team_id must be a string'),
        ({'reason': 5}, 'reason must be a string'),
        ({'reason': 'x' * 256}, 'reason must be at most 255 characters'),
    ])
    def test_bad_body(self, client, seeded, body, error):
        mid = seeded[('T002', 'T004')]
        payload = {'tournament_id': T, 'winner_team_id': 'T004'}
        payload.update(body)
        response = client.post(f'/declare-walkover/{mid}', json=payload)
        assert response.status_code == 400
        assert response.get_json()['error'] == error

    def test_non_json_body(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        response = client.post(
            f'/declare-walkover/{mid}', data='nope', content_type='text/plain'
        )
        assert response.status_code == 400

    def test_winner_not_in_match(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        response = declare(client, mid, winner_team_id='T001')
        assert response.status_code == 400
        assert 'T002 or T004' in response.get_json()['error']

    def test_missing_match(self, client, seeded):
        response = declare(client, 99999)
        assert response.status_code == 404

    def test_match_from_another_tournament(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        response = declare(client, mid, tournament_id=2)
        assert response.status_code == 404

    @pytest.mark.parametrize('status', ['on-going', 'completed'])
    def test_started_or_completed_match(self, client, seeded, status):
        mid = seeded[('T002', 'T004')]
        if status != 'completed':
            client.post(f'/update-match-status/{mid}', json={
                'tournament_id': T, 'status': status
            })
        else:
            # 0-0 with is_final marks a finalized match without a scoreline.
            match = db.session.get(Match, mid)
            match.is_final = True
            match.status = 'completed'
            db.session.commit()

        response = declare(client, mid)
        assert response.status_code == 409
        assert 'Only pending matches' in response.get_json()['error']

    def test_regularly_finalized_match(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        assert client.post('/update-score', json={
            'match_id': mid, 'tournament_id': T, 'score': '11-3', 'final': True
        }).status_code == 200
        assert declare(client, mid).status_code == 409

    def test_different_winner_after_walkover(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200
        response = declare(client, mid, winner_team_id='T002')
        assert response.status_code == 409
        db.session.expire_all()
        assert db.session.get(Match, mid).winner_team_id == 'T004'

    def test_opponent_tbd(self, client, seeded):
        bracket = create_knockout(client, [('T001', 'T005'), ('T003', 'T007')])
        response = declare(client, bracket['F1']['id'], winner_team_id='T005')
        assert response.status_code == 409
        assert 'Both teams must be known' in response.get_json()['error']


class TestScoringIsBlocked:
    def test_update_score_rejected(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        response = client.post('/update-score', json={
            'match_id': mid, 'tournament_id': T, 'score': '11-0', 'final': True
        })
        assert response.status_code == 409
        assert response.get_json()['error'] == 'Scores cannot be recorded for a walkover match'

        scores = Score.query.filter_by(match_id=mid).all()
        assert {s.score for s in scores} == {0}

    @pytest.mark.parametrize('status', ['on-going', 'pending'])
    def test_status_change_rejected(self, client, seeded, status):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        response = client.post(f'/update-match-status/{mid}', json={
            'tournament_id': T, 'status': status
        })
        assert response.status_code == 409
        assert 'cannot be changed' in response.get_json()['error']
        db.session.expire_all()
        assert db.session.get(Match, mid).status == 'completed'

    def test_setting_same_status_is_harmless(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        response = client.post(f'/update-match-status/{mid}', json={
            'tournament_id': T, 'status': 'completed'
        })
        assert response.status_code == 200


class TestAtomicity:
    def test_failure_in_bracket_advancement_leaves_match_untouched(
        self, client, seeded, monkeypatch
    ):
        bracket = create_knockout(client, [('T001', 'T005'), ('T003', 'T007')])
        sf1 = bracket['SF1']['id']

        def boom(*args, **kwargs):
            raise RuntimeError('successor update failed')

        monkeypatch.setattr(score_core, 'update_successor_match', boom)

        response = declare(client, sf1, winner_team_id='T005')
        assert response.status_code == 500

        db.session.expire_all()
        match = db.session.get(Match, sf1)
        assert match.outcome_type == 'REGULAR'
        assert match.winner_team_id is None
        assert match.is_final is False
        assert match.status == 'pending'


class TestStandingsAndExports:
    def test_walkover_counts_as_win_with_no_points(self, client, seeded):
        before_t4 = standings_row(client, 'T004')
        before_t1 = standings_row(client, 'T001')

        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        after_t4 = standings_row(client, 'T004')
        after_t1 = standings_row(client, 'T001')

        assert after_t4['matches_won'] == before_t4['matches_won'] + 1
        assert after_t4['total_scores'] == before_t4['total_scores'] + 2
        assert after_t4['matches_played'] == before_t4['matches_played'] + 1
        assert after_t4['points_scored'] == before_t4['points_scored']
        assert after_t4['matches_lost'] == (
            after_t4['matches_played'] - after_t4['matches_won']
        )
        assert after_t1 == before_t1

    def test_unplayed_regular_match_still_not_counted(self, client, seeded):
        row = standings_row(client, 'T002')
        assert row['matches_won'] == 0
        assert row['total_scores'] == 0

    def test_overall_and_second_place_standings(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        walkover = declare(client, mid).get_json()['match']
        assert walkover['winner_team_id'] == 'T004'

        overall = client.get(f'/overall-standings/{T}').get_json()['standings']
        t4 = next(r for r in overall if r['team_id'] == 'T004')
        assert t4['matches_won'] >= 1
        assert t4['total_scores'] == t4['matches_won'] * 2

        second = client.get(f'/second-place-standings/{T}').get_json()
        assert second['total_second_place_teams'] == len(second['standings'])
        for r in second['standings']:
            assert r['total_scores'] == r['matches_won'] * 2

    def test_knockout_top_teams(self, client, seeded):
        def pool_a():
            return client.get(
                f'/knockout-top-teams?tournament_id={T}&round_id=1&teams_per_pool=4'
            ).get_json()['top_teams']['A']

        before = pool_a()
        opponent = next(t for t in before if t['team_id'] == 'T002')
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200
        after = pool_a()

        ranking = [t['team_id'] for t in after]
        t4 = next(t for t in after if t['team_id'] == 'T004')
        assert t4['matches_won'] >= 1
        # The walkover win moves T004 above T002, who lost the same fixture.
        if 'T004' in ranking and 'T002' in ranking:
            assert ranking.index('T004') < ranking.index('T002')
        assert opponent['matches_won'] <= next(
            t for t in after if t['team_id'] == 'T002'
        )['matches_won']

    def test_dupr_export_skips_walkover(self, client, seeded):
        regular = add_pending_match('T001', 'T002')
        walkover = add_pending_match('T001', 'T004')

        assert client.post('/update-score', json={
            'match_id': regular, 'tournament_id': T, 'score': '11-4', 'final': True
        }).status_code == 200
        assert declare(client, walkover).status_code == 200

        text = client.get(f'/export-tournament-csv?tournament_id={T}').get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(text)))
        # Header plus only the regular match: the walkover is not rating data.
        assert len(rows) == 2
        assert rows[0][0] == ''

    def test_fixtures_csv_shows_walkover(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        assert declare(client, mid).status_code == 200

        text = client.get(f'/get-match-fixtures/csv?tournament_id={T}').get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(text)))
        assert any('W/O (T004)' in r for r in rows)


class TestGenericDeclarationRoute:
    def test_generic_route_declares_walkover(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        response = client.post(f'/declare-outcome/{mid}', json={
            'tournament_id': T,
            'outcome': 'WALKOVER',
            'winner_team_id': 'T004'
        })
        assert response.status_code == 200
        body = response.get_json()
        assert body['message'] == 'Walkover declared successfully'
        assert body['match']['outcome']['type'] == 'WALKOVER'

    def test_generic_route_is_idempotent(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        payload = {'tournament_id': T, 'outcome': 'walkover', 'winner_team_id': 'T004'}
        assert client.post(f'/declare-outcome/{mid}', json=payload).status_code == 200
        again = client.post(f'/declare-outcome/{mid}', json=payload)
        assert again.status_code == 200
        assert again.get_json()['message'] == 'Walkover already recorded'

    def test_missing_outcome(self, client, seeded):
        mid = seeded[('T002', 'T004')]
        response = client.post(f'/declare-outcome/{mid}', json={
            'tournament_id': T, 'winner_team_id': 'T004'
        })
        assert response.status_code == 400
        assert response.get_json()['error'] == 'outcome is required'

    @pytest.mark.parametrize('outcome,error', [
        ('BOGUS', 'Unknown outcome type: BOGUS'),
        ('REGULAR', 'Regular matches cannot be declared directly'),
    ])
    def test_unsupported_outcomes_are_rejected(self, client, seeded, outcome, error):
        mid = seeded[('T002', 'T004')]
        response = client.post(f'/declare-outcome/{mid}', json={
            'tournament_id': T, 'outcome': outcome, 'winner_team_id': 'T004'
        })
        assert response.status_code == 400
        assert response.get_json()['error'] == error
