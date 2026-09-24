from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.schemas import AIExtraction, AIItemFields
from tests.conftest import FunctionAIService
from tests.test_deadline_ranking import NOW, item, ranked


def capture(client):
    response = client.post('/api/inputs', json={'original_text': 'keep this thought'})
    assert response.status_code == 202
    return client.get('/api/items').json()['sortable_items'][0]['id']


def test_pin_api_validation_lifecycle_and_reminder_independence(client_factory):
    client = client_factory(FunctionAIService(lambda *_: AIExtraction(fields=AIItemFields(title='thought'))))
    item_id = capture(client)
    url = f'/api/items/{item_id}'
    assert client.get(url).json()['item']['is_pinned'] is False
    for invalid in (None, 0, 1, 'true', 'false'):
        assert client.patch(url, json={'is_pinned': invalid}).status_code == 422
    for state in ('active', 'completed', 'trash', None):
        assert client.patch(url, json={'is_pinned': True, 'status': state}).status_code == 409
    assert client.patch(url, json={'is_pinned': True}).json()['is_pinned'] is True
    assert client.patch(url, json={'title': 'edited'}).json()['is_pinned'] is True
    assert client.patch(url, json={'deadline': '2026-09-21'}).status_code == 409
    for state in ('completed', 'trash', 'active', 'active'):
        response = client.patch(url, json={'status': state})
        assert response.status_code == 200
        row = response.json()
        assert row['is_pinned'] is True
        if row['status'] != 'active':
            for pin in (True, False):
                assert client.patch(url, json={'is_pinned': pin}).status_code == 409
        dashboard = client.get('/api/items', params={'status': row['status']}).json()
        assert dashboard['sortable_items'][0]['is_pinned'] is True
    assert client.patch(url, json={'is_pinned': False}).json()['is_pinned'] is False
    assert client.patch('/api/items/99999', json={'is_pinned': True}).status_code == 404
    assert client.get(url).json()['reminder'] is None
    # Active -> Trash -> restore also retains Pin without passing through History.
    client.patch(url, json={'is_pinned': True})
    for action in ('move_to_trash', 'restore_from_trash', 'complete',
                   'move_to_trash', 'restore_from_trash', 'restore_to_current'):
        response = client.post('/api/items/bulk-lifecycle', json={'item_ids': [item_id], 'action': action})
        assert response.status_code == 200
        assert client.get(url).json()['item']['is_pinned'] is True


def test_ai_update_reprocess_cannot_change_pin(client_factory):
    client = client_factory(FunctionAIService(lambda *_: AIExtraction(fields=AIItemFields(title='AI title'))))
    item_id = capture(client)
    url = f'/api/items/{item_id}'
    for pin in (True, False):
        assert client.patch(url, json={'is_pinned': pin}).status_code == 200
        assert client.post(url + '/inputs', json={'original_text': 'pin or unpin this'}).status_code == 202
        assert client.post(url + '/reprocess').json()['is_pinned'] is pin
        assert client.get(url).json()['item']['is_pinned'] is pin
    with pytest.raises(ValidationError):
        AIItemFields.model_validate({'is_pinned': True})


def test_pin_groups_reuse_deadline_precision_and_determinism():
    deadlines = ['2026-09-20T11:00:00', '2026-09-19T23:00:00',
                 '2026-09-19', '2026-09-20T13:00:00', '2026-09-20',
                 '2026-09-21T08:00:00', '2026-09-21', None, None]
    rows = [item(i, value, created=NOW - timedelta(seconds=i))
            for i, value in enumerate(deadlines, 1)]
    pinned = [row.model_copy(update={'id': row.id + 100, 'is_pinned': True}) for row in rows]
    assert ranked((rows + pinned)[::-1]) == list(range(101, 110)) + list(range(1, 10))
    changed = [row.model_copy(update={'updated_time': NOW + timedelta(days=row.id),
                                     'importance': 'high', 'urgency': 'high'}) for row in pinned]
    assert ranked(changed + rows) == ranked(pinned + rows)
    assert ranked([item(2, is_pinned=True), item(1, is_pinned=True)]) == [1, 2]
    assert ranked([item(2, status='completed', is_pinned=True), item(1)]) == [1, 2]


def test_pagination_ranks_pin_before_slicing_and_history_ignores_pin(client_factory):
    client = client_factory(FunctionAIService(lambda *_: AIExtraction(fields=AIItemFields(title='thought'))))
    item_id = capture(client)
    repository = client.app.state.repository
    with repository.database.transaction() as connection:
        for index in range(101):
            connection.execute('''INSERT INTO personal_items
                (user_id,title,type,importance,urgency,status,created_time,updated_time)
                SELECT user_id, ?, type, importance, urgency, status, ?, updated_time
                FROM personal_items WHERE id=?''', (str(index), '2026-09-21T00:00:00+00:00', item_id))
        connection.execute("UPDATE personal_items SET created_time='2020-01-01T00:00:00+00:00' WHERE id=?", (item_id,))
    assert client.patch(f'/api/items/{item_id}', json={'is_pinned': True}).status_code == 200
    all_rows = client.get('/api/items').json()['sortable_items']
    page1 = client.get('/api/items?page=1').json()
    page2 = client.get('/api/items?page=2').json()
    assert all_rows[0]['id'] == item_id
    assert page1['total_items'] == 102 and page1['total_pages'] == 2
    assert page1['sortable_items'] + page2['sortable_items'] == all_rows
    with repository.database.transaction() as connection:
        connection.execute("UPDATE personal_items SET status='completed', completed_at=created_time")
    history = client.get('/api/items?status=completed').json()['sortable_items']
    assert history[-1]['id'] == item_id and history[-1]['is_pinned'] is True
    with repository.database.transaction() as connection:
        connection.execute("UPDATE personal_items SET status='trash', trashed_at=created_time")
    trash = client.get('/api/items?status=trash').json()['sortable_items']
    assert trash[-1]['id'] == item_id and trash[-1]['is_pinned'] is True
