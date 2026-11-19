"""Unit tests for the outcome abstraction in outcomes.py."""

import pytest

from models import Match
from outcomes import (
    MatchOutcome,
    OUTCOME_HANDLERS,
    OutcomeError,
    OutcomeHandler,
    RegularOutcome,
    WalkoverOutcome,
    declare_outcome,
    get_handler,
    outcome_payload,
    tally_match,
)


def v0_inline_rules(team1_id, team2_id, s1, s2):
    """The rules v0 hard-coded in every standings function, used as the reference."""
    counted = not (s1 == 0 and s2 == 0)
    if s1 > s2:
        winner = team1_id
    elif s2 > s1:
        winner = team2_id
    else:
        winner = None
    return counted, winner


class TestRegistry:
    def test_every_outcome_has_a_handler(self):
        for outcome in MatchOutcome:
            handler = OUTCOME_HANDLERS[outcome]
            assert isinstance(handler, OutcomeHandler)
            assert handler.outcome is outcome

    @pytest.mark.parametrize('value', [None, '', 'FORFEIT', 'regular', 'walkover'])
    def test_unknown_or_legacy_values_fall_back_to_regular(self, value):
        assert get_handler(value) is OUTCOME_HANDLERS[MatchOutcome.REGULAR]

    def test_lookup_by_string_and_enum(self):
        assert get_handler('REGULAR') is OUTCOME_HANDLERS[MatchOutcome.REGULAR]
        assert get_handler('WALKOVER') is OUTCOME_HANDLERS[MatchOutcome.WALKOVER]
        assert get_handler(MatchOutcome.WALKOVER) is OUTCOME_HANDLERS[MatchOutcome.WALKOVER]


class TestRegularTally:
    @pytest.mark.parametrize('s1,s2', [(11, 7), (7, 11), (10, 10), (0, 0), (0, 11)])
    def test_matches_v0_rules_exactly(self, s1, s2):
        tally = RegularOutcome().tally('A', 'B', s1, s2, None)
        counted, winner = v0_inline_rules('A', 'B', s1, s2)
        assert tally.counted is counted
        assert tally.winner_team_id == winner
        assert tally.team1_points == s1
        assert tally.team2_points == s2
        assert tally.points_for('A') == s1
        assert tally.points_for('B') == s2
        assert tally.points_for('missing', 99) == 99

    def test_regular_ignores_stored_winner_like_v0(self):
        # v0 re-derived the winner from the score every time.
        tally = RegularOutcome().tally('A', 'B', 11, 7, 'B')
        assert tally.winner_team_id == 'A'

    def test_tally_match_normalizes_missing_scores(self):
        tally = tally_match('REGULAR', 'A', 'B', None, None, None)
        assert tally.counted is False
        assert tally.team1_points == 0
        assert tally.team2_points == 0


class TestWalkoverTally:
    def test_winner_is_counted_with_no_points(self):
        tally = WalkoverOutcome().tally('A', 'B', 0, 0, 'B')
        assert tally.counted is True
        assert tally.winner_team_id == 'B'
        assert tally.points_for('A') == 0
        assert tally.points_for('B') == 0

    @pytest.mark.parametrize('winner', [None, '', 'TBD', 'C'])
    def test_invalid_winner_is_not_counted(self, winner):
        tally = WalkoverOutcome().tally('A', 'B', 0, 0, winner)
        assert tally.counted is False
        assert tally.winner_team_id is None


class TestOutcomePayload:
    def test_regular_payload(self):
        match = Match(team1_id='A', team2_id='B', outcome_type=None)
        assert outcome_payload(match) == {
            'type': 'REGULAR', 'reason': None, 'scores_applicable': True
        }

    def test_walkover_payload(self):
        match = Match(team1_id='A', team2_id='B', outcome_type='WALKOVER', outcome_reason='hurt')
        assert outcome_payload(match) == {
            'type': 'WALKOVER', 'reason': 'hurt', 'scores_applicable': False
        }


class TestDeclarationRules:
    @pytest.mark.usefixtures('app')
    def test_regular_cannot_be_declared(self):
        match = Match(team1_id='A', team2_id='B', outcome_type='REGULAR')
        with pytest.raises(OutcomeError) as exc:
            declare_outcome(match, MatchOutcome.REGULAR, 'A', None)
        assert exc.value.status_code == 400

    def test_walkover_requires_known_teams(self):
        match = Match(team1_id='A', team2_id='TBD', outcome_type='REGULAR')
        with pytest.raises(OutcomeError) as exc:
            WalkoverOutcome().validate_declaration(match, 'A')
        assert exc.value.status_code == 409

    def test_walkover_winner_must_be_in_match(self):
        match = Match(team1_id='A', team2_id='B', outcome_type='REGULAR')
        with pytest.raises(OutcomeError) as exc:
            WalkoverOutcome().validate_declaration(match, 'C')
        assert exc.value.status_code == 400

    def test_walkover_only_from_pending(self):
        match = Match(team1_id='A', team2_id='B', outcome_type='REGULAR', status='on-going')
        with pytest.raises(OutcomeError) as exc:
            WalkoverOutcome().validate_declaration(match, 'A')
        assert exc.value.status_code == 409

    def test_completed_regular_match_cannot_become_a_walkover(self):
        match = Match(team1_id='A', team2_id='B', outcome_type='REGULAR', is_final=True)
        with pytest.raises(OutcomeError) as exc:
            WalkoverOutcome().validate_declaration(match, 'A')
        assert exc.value.status_code == 409


class TestExtensibility:
    def test_winner_rule_lives_on_the_handler(self):
        handler = get_handler('REGULAR')
        assert handler.winner_from_scores('A', 'B', 11, 3) == 'A'
        assert handler.winner_from_scores('A', 'B', 3, 11) == 'B'
        assert handler.winner_from_scores('A', 'B', 0, 0) is None

    def test_declarable_handlers_report_their_own_label(self):
        assert get_handler('WALKOVER').display_name == 'Walkover'
        assert get_handler('REGULAR').display_name == 'Regular'
