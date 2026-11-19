"""
Match outcomes: HOW a match was decided.

This is the single place that knows what each outcome means. Routes and
standings code never compare outcome strings themselves; they ask the handler.

Three separate concepts on a match:
  * status        - lifecycle: pending -> on-going -> completed (unchanged from v0)
  * scores        - the `score` table, one total per team (unchanged from v0)
  * outcome_type  - how the result was reached: REGULAR (played and scored)
                    or WALKOVER (one side did not play)

To add an outcome (e.g. FORFEIT or BYE):
  1. add a member to MatchOutcome,
  2. write an OutcomeHandler subclass describing its rules,
  3. register an instance in OUTCOME_HANDLERS,
  4. add a label to the frontend map (v0_frontend/src/utils/matchOutcome.js).

Declarable outcomes are exposed through the generic /declare-outcome route, so
a new outcome needs no new backend route. The referee UI reads its declaration
buttons from the same frontend map.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from models import Score, db


class MatchOutcome(str, Enum):
    REGULAR = 'REGULAR'
    WALKOVER = 'WALKOVER'


# Legacy rows (and any unknown value) are read as a played match.
DEFAULT_OUTCOME = MatchOutcome.REGULAR

# Team slots on a knockout match before the feeding match has been decided.
PLACEHOLDER_TEAM_IDS = (None, '', 'TBD')


class OutcomeError(Exception):
    """A rule violation when declaring an outcome. Routes turn it into {"error": message}."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class MatchTally:
    """What a single match contributes to standings, promotion and points tables."""

    counted: bool
    winner_team_id: Optional[str]
    team1_id: Optional[str]
    team2_id: Optional[str]
    team1_points: int
    team2_points: int

    def points_for(self, team_id, default=0):
        if team_id == self.team1_id:
            return self.team1_points
        if team_id == self.team2_id:
            return self.team2_points
        return default


class OutcomeHandler(ABC):
    """Rules for one outcome type."""

    outcome = None
    scores_applicable = True
    allows_status_update = True
    include_in_rating_export = True
    declarable = False
    short_label = None

    @property
    def display_name(self):
        return self.outcome.value.replace('_', ' ').title()

    def winner_from_scores(self, team1_id, team2_id, team1_score, team2_score):
        """How a scored match decides its winner.

        This is the default v0 rule. A scored outcome with different rules only
        needs to override this method; the score route calls it instead of
        comparing scores itself.
        """
        if team1_score > team2_score:
            return team1_id
        if team2_score > team1_score:
            return team2_id
        return None

    @abstractmethod
    def tally(self, team1_id, team2_id, team1_score, team2_score, winner_team_id):
        ...

    def validate_declaration(self, match, winner_team_id):
        raise OutcomeError(f"{self.outcome.value} matches cannot be declared directly", 400)

    def apply(self, match, winner_team_id, reason):
        raise OutcomeError(f"{self.outcome.value} matches cannot be declared directly", 400)


class RegularOutcome(OutcomeHandler):
    """A played match. The rules are exactly the ones v0 already used everywhere."""

    outcome = MatchOutcome.REGULAR

    def tally(self, team1_id, team2_id, team1_score, team2_score, winner_team_id):
        # v0 treated 0-0 as "not played yet" (and also as a legitimate draw).
        counted = not (team1_score == 0 and team2_score == 0)
        winner = self.winner_from_scores(
            team1_id, team2_id, team1_score, team2_score
        )
        return MatchTally(counted, winner, team1_id, team2_id, team1_score, team2_score)


class WalkoverOutcome(OutcomeHandler):
    """One side wins because the other side did not play."""

    outcome = MatchOutcome.WALKOVER
    scores_applicable = False
    allows_status_update = False
    include_in_rating_export = False
    declarable = True
    short_label = 'W/O'

    # The single place that decides what a walkover records in the score table.
    # (0, 0) mirrors an unplayed match: the winner is known, no points were
    # played. Change this tuple to record a different convention everywhere.
    AWARDED_SCORE = (0, 0)

    # A walkover is declared before play starts. It cannot replace a result.
    DECLARABLE_FROM_STATUSES = ('pending',)

    def tally(self, team1_id, team2_id, team1_score, team2_score, winner_team_id):
        if winner_team_id not in (team1_id, team2_id) or winner_team_id in PLACEHOLDER_TEAM_IDS:
            return MatchTally(False, None, team1_id, team2_id, 0, 0)
        winner_points, loser_points = self.AWARDED_SCORE
        if winner_team_id == team1_id:
            return MatchTally(True, team1_id, team1_id, team2_id, winner_points, loser_points)
        return MatchTally(True, team2_id, team1_id, team2_id, loser_points, winner_points)

    def validate_declaration(self, match, winner_team_id):
        if match.team1_id in PLACEHOLDER_TEAM_IDS or match.team2_id in PLACEHOLDER_TEAM_IDS:
            raise OutcomeError('Both teams must be known before a walkover can be declared', 409)

        if winner_team_id not in (match.team1_id, match.team2_id):
            raise OutcomeError(
                f'winner_team_id must be one of the teams in this match ({match.team1_id} or {match.team2_id})',
                400
            )

        status = match.status or 'pending'
        if match.is_final or status not in self.DECLARABLE_FROM_STATUSES:
            state = 'completed' if match.is_final else status
            raise OutcomeError(
                f'Only pending matches can be declared a walkover (this match is {state})',
                409
            )

        recorded = Score.query.filter(
            Score.match_id == match.id,
            Score.team_id.in_([match.team1_id, match.team2_id]),
            Score.score != 0
        ).first()
        if recorded:
            raise OutcomeError(
                'Scores have already been recorded for this match, so it cannot be a walkover',
                409
            )

    def apply(self, match, winner_team_id, reason):
        winner_points, loser_points = self.AWARDED_SCORE

        for team_id in (match.team1_id, match.team2_id):
            awarded = winner_points if team_id == winner_team_id else loser_points
            score_row = Score.query.filter_by(
                match_id=match.id, team_id=team_id, tournament_id=match.tournament_id
            ).first()
            if not score_row:
                # v0 normally creates these rows with the match; keep the
                # declaration working for matches imported without them.
                score_row = Score(
                    match_id=match.id, team_id=team_id,
                    tournament_id=match.tournament_id, score=awarded
                )
                db.session.add(score_row)
            else:
                score_row.score = awarded

        match.outcome_type = self.outcome.value
        match.outcome_reason = reason
        match.winner_team_id = winner_team_id
        match.is_final = True
        match.status = 'completed'


OUTCOME_HANDLERS = {
    handler.outcome: handler
    for handler in (RegularOutcome(), WalkoverOutcome())
}


def get_handler(outcome_type):
    try:
        return OUTCOME_HANDLERS[MatchOutcome(outcome_type)]
    except ValueError:
        return OUTCOME_HANDLERS[DEFAULT_OUTCOME]


def parse_outcome(value):
    """Turn a client-supplied outcome name into a registered enum member."""
    if not isinstance(value, str):
        raise OutcomeError('outcome must be a string', 400)
    try:
        return MatchOutcome(value.strip().upper())
    except ValueError:
        raise OutcomeError(f'Unknown outcome type: {value}', 400)


def get_declarable_handler(outcome):
    """Return a declarable handler, or reject the request centrally."""
    outcome = outcome if isinstance(outcome, MatchOutcome) else parse_outcome(outcome)
    handler = OUTCOME_HANDLERS[outcome]
    if not handler.declarable:
        raise OutcomeError(f'{handler.display_name} matches cannot be declared directly', 400)
    return handler


def outcome_label(outcome_type):
    return get_handler(outcome_type).display_name


def stored_outcome(match):
    return match.outcome_type or DEFAULT_OUTCOME.value


def tally_match(outcome_type, team1_id, team2_id, team1_score, team2_score, winner_team_id):
    return get_handler(outcome_type).tally(
        team1_id, team2_id,
        team1_score or 0, team2_score or 0,
        winner_team_id
    )


def outcome_payload(match):
    return {
        'type': stored_outcome(match),
        'reason': match.outcome_reason,
        'scores_applicable': get_handler(match.outcome_type).scores_applicable,
    }


def declare_outcome(match, outcome, winner_team_id, reason):
    # Imported here because score_core imports this module.
    from routes.score.score_core import update_successor_match

    outcome = outcome if isinstance(outcome, MatchOutcome) else parse_outcome(outcome)
    handler = OUTCOME_HANDLERS[outcome]
    if not handler.declarable:
        raise OutcomeError(f"{handler.outcome.value} cannot be declared directly", 400)

    current = stored_outcome(match)
    if current == handler.outcome.value:
        if match.winner_team_id == winner_team_id:
            return False
        raise OutcomeError(
            f"This match is already a {handler.outcome.value.lower()} won by {match.winner_team_id}",
            409
        )
    if current != DEFAULT_OUTCOME.value:
        raise OutcomeError(f"This match already has outcome {current}", 409)

    handler.validate_declaration(match, winner_team_id)
    handler.apply(match, winner_team_id, reason)

    if match.successor:
        update_successor_match(match.successor, match.id, winner_team_id, commit=False)
    return True
