"""
Shared pytest fixtures.

The tests build their own Flask app instead of importing app.py, because app.py
monkey-patches gevent and calls db.create_all() against MySQL at import time.
The real blueprints are registered through routes.initialize_routes, so every
request goes through the same view functions the server uses. The database is
an in-memory SQLite DB, which accepts the backtick-quoted `match` table used in
the raw SQL standings queries.
"""

import os
import sys

import pytest
from flask import Flask
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import db, SuperTournament, Season, Tournament, Team, Player, Match, Score, Round
from routes import initialize_routes
from socket_instance import socketio


def _build_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_ENGINE_OPTIONS={
            'connect_args': {'check_same_thread': False},
            'poolclass': StaticPool,
        },
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    initialize_routes(app)
    return app


_APP = _build_app()


@pytest.fixture
def app(monkeypatch):
    emitted = []
    monkeypatch.setattr(socketio, 'emit', lambda *args, **kwargs: emitted.append((args, kwargs)))
    _APP.config['EMITTED'] = emitted
    with _APP.app_context():
        db.create_all()
        yield _APP
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def emitted(app):
    return app.config['EMITTED']


TOURNAMENT_ID = 1
T = TOURNAMENT_ID

POOL_A = ['T001', 'T002', 'T003', 'T004']
POOL_B = ['T005', 'T006', 'T007', 'T008']

# (team1, team2, team1_score, team2_score). 0-0 means "not played" in v0.
POOL_A_RESULTS = [
    ('T001', 'T002', 11, 7),
    ('T001', 'T003', 9, 11),
    ('T001', 'T004', 11, 3),
    ('T002', 'T003', 10, 10),
    ('T002', 'T004', 0, 0),
    ('T003', 'T004', 11, 8),
]

POOL_B_RESULTS = [
    ('T005', 'T006', 11, 4),
    ('T005', 'T007', 11, 9),
    ('T005', 'T008', 0, 0),
    ('T006', 'T007', 6, 11),
    ('T006', 'T008', 11, 2),
    ('T007', 'T008', 0, 0),
]


def seed_tournament():
    db.session.add(SuperTournament(id=1, name='Assignment', description='assignment'))
    db.session.add(Season(id=1, name='S1', super_tournament_id=1))
    db.session.add(Tournament(
        id=TOURNAMENT_ID, tournament_name='Test Open', type='elimination',
        num_courts=2, season_id=1
    ))

    for index, team_id in enumerate(POOL_A + POOL_B, start=1):
        p1 = f'P{index:03d}A'
        p2 = f'P{index:03d}B'
        for uuid, first in ((p1, f'Alpha{index}'), (p2, f'Beta{index}')):
            db.session.add(Player(
                uuid=uuid, first_name=first, last_name='Player', gender='Male', age=30,
                phone_number=f'99999{index:03d}{len(uuid)}',
                email=f'{uuid}@x.test', skill_type='INTERMEDIATE',
                dupr_id=f'D{uuid}', super_tournament_id=1
            ))
        db.session.add(Team(
            team_id=team_id, name=f'Team {team_id}', tournament_id=TOURNAMENT_ID,
            player1_uuid=p1, player2_uuid=p2
        ))

    for pool, teams in (('A', POOL_A), ('B', POOL_B)):
        for team_id in teams:
            db.session.add(Round(
                round_id=1, team_id=team_id, pool=pool,
                name='Round Robin', tournament_id=TOURNAMENT_ID
            ))
    db.session.flush()

    match_ids = {}
    for pool, results in (('A', POOL_A_RESULTS), ('B', POOL_B_RESULTS)):
        for team1, team2, s1, s2 in results:
            match = Match(
                match_name=f'Round 1 Pool {pool} - {team1} vs {team2}',
                team1_id=team1, team2_id=team2, round_id='1', pool=pool,
                tournament_id=TOURNAMENT_ID, status='pending'
            )
            db.session.add(match)
            db.session.flush()
            db.session.add(Score(match_id=match.id, team_id=team1, score=s1, tournament_id=TOURNAMENT_ID))
            db.session.add(Score(match_id=match.id, team_id=team2, score=s2, tournament_id=TOURNAMENT_ID))
            match_ids[(team1, team2)] = match.id
    db.session.commit()
    return match_ids


def add_pending_match(team1, team2, pool='A', round_id='1'):
    match = Match(
        match_name=f'Extra - {team1} vs {team2}', team1_id=team1, team2_id=team2,
        round_id=round_id, pool=pool, tournament_id=TOURNAMENT_ID, status='pending'
    )
    db.session.add(match)
    db.session.flush()
    for team_id in (team1, team2):
        db.session.add(Score(match_id=match.id, team_id=team_id, score=0, tournament_id=TOURNAMENT_ID))
    db.session.commit()
    return match.id


@pytest.fixture
def seeded(app):
    return seed_tournament()


def create_knockout(client, pairs):
    response = client.post('/knockout-from-matches', json={
        'tournament_id': TOURNAMENT_ID,
        'matches': [{'team1_id': a, 'team2_id': b} for a, b in pairs],
        'current_round_id': 1,
    })
    assert response.status_code == 201
    return {m['name']: m for m in response.get_json()['matches']}
