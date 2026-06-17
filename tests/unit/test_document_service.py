"""Tests for DocumentService (item #3)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)


_TABLE = 'semantic-layer-metadata'
_BUCKET = 'semantic-layer-test-artifacts'


def _build_table():
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=_TABLE,
        AttributeDefinitions=[
            {'AttributeName': 'id', 'AttributeType': 'S'},
            {'AttributeName': 'version', 'AttributeType': 'S'},
        ],
        KeySchema=[
            {'AttributeName': 'id', 'KeyType': 'HASH'},
            {'AttributeName': 'version', 'KeyType': 'RANGE'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )


def _build_bucket():
    s3 = boto3.client('s3', region_name='us-east-1')
    s3.create_bucket(Bucket=_BUCKET)


@pytest.fixture
def service():
    from services.document_service import DocumentService
    with mock_aws():
        _build_table()
        _build_bucket()
        yield DocumentService(
            table_name=_TABLE, bucket=_BUCKET, region='us-east-1'
        )


def test_upload_persists_to_s3_and_ddb(service):
    item = service.upload_document(
        ontology_id='ont-1', filename='spec.md', body=b'# Hello\n\ncontent'
    )
    assert item['filename'] == 'spec.md'
    assert item['stages']['chunked'] is False
    fetched = service.get_document(
        ontology_id='ont-1', doc_id=item['docId']
    )
    assert fetched is not None
    assert fetched['ontologyId'] == 'ont-1'


def test_upload_rejects_unsupported_extension(service):
    from services.document_service import DocumentValidationError
    with pytest.raises(DocumentValidationError):
        service.upload_document(
            ontology_id='ont-1', filename='exec.exe', body=b'x'
        )


def test_upload_rejects_oversized_body(service, monkeypatch):
    import services.document_service as ds_mod
    monkeypatch.setattr(ds_mod, 'MAX_UPLOAD_BYTES', 10)
    from services.document_service import DocumentValidationError
    with pytest.raises(DocumentValidationError):
        service.upload_document(
            ontology_id='ont-1', filename='big.txt', body=b'x' * 100
        )


def test_upload_rejects_when_count_cap_reached(service, monkeypatch):
    import services.document_service as ds_mod
    monkeypatch.setattr(ds_mod, 'MAX_DOCS_PER_ONTOLOGY', 2)
    service.upload_document(
        ontology_id='o', filename='a.txt', body=b'a'
    )
    service.upload_document(
        ontology_id='o', filename='b.txt', body=b'b'
    )
    from services.document_service import DocumentValidationError
    with pytest.raises(DocumentValidationError):
        service.upload_document(
            ontology_id='o', filename='c.txt', body=b'c'
        )


def test_list_documents_returns_only_for_target_ontology(service):
    service.upload_document(
        ontology_id='ont-1', filename='a.txt', body=b'a'
    )
    service.upload_document(
        ontology_id='ont-1', filename='b.txt', body=b'b'
    )
    service.upload_document(
        ontology_id='ont-2', filename='c.txt', body=b'c'
    )
    items = service.list_documents(ontology_id='ont-1')
    assert len(items) == 2


def test_update_stage_marks_success(service):
    item = service.upload_document(
        ontology_id='o', filename='a.txt', body=b'a'
    )
    updated = service.update_stage(
        ontology_id='o', doc_id=item['docId'], stage='chunked', success=True
    )
    assert updated['stages']['chunked'] is True


def test_update_stage_records_error(service):
    item = service.upload_document(
        ontology_id='o', filename='a.txt', body=b'a'
    )
    updated = service.update_stage(
        ontology_id='o',
        doc_id=item['docId'],
        stage='embedded',
        success=False,
        error='Bedrock throttled',
    )
    assert updated['stages']['embedded'] is False
    assert updated['errors']['embedded'] == 'Bedrock throttled'


def test_delete_document_removes_row(service):
    item = service.upload_document(
        ontology_id='o', filename='a.txt', body=b'a'
    )
    service.delete_document(ontology_id='o', doc_id=item['docId'])
    assert service.get_document(
        ontology_id='o', doc_id=item['docId']
    ) is None


def test_delete_missing_document_is_noop(service):
    # Should not raise.
    service.delete_document(ontology_id='o', doc_id='no-such-id')


def test_upload_starts_state_machine_when_arn_set():
    """Upload must call states:StartExecution against the doc-pipeline SM
    when the ARN is wired (item #3 critical fix). Without this, every
    uploaded doc sits forever in 'chunked: false'."""
    from unittest.mock import MagicMock
    from services.document_service import DocumentService
    with mock_aws():
        _build_table()
        _build_bucket()
        sfn = MagicMock()
        sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:us-east-1:0:execution/x/y'
        }
        service = DocumentService(
            table_name=_TABLE,
            bucket=_BUCKET,
            region='us-east-1',
            state_machine_arn='arn:aws:states:us-east-1:0:stateMachine/doc-pipeline',
            sfn_client=sfn,
        )
        item = service.upload_document(
            ontology_id='o', filename='spec.md', body=b'# hi\n\ncontent'
        )
        assert item.get('executionStarted') is True
        sfn.start_execution.assert_called_once()
        args = sfn.start_execution.call_args.kwargs
        assert args['stateMachineArn'].endswith('/doc-pipeline')
        # Execution name embeds docId for traceability.
        assert args['name'].startswith('doc-')


def test_upload_swallows_state_machine_failure():
    """If states:StartExecution fails, upload still succeeds and the
    error is recorded — operator can retry."""
    from unittest.mock import MagicMock
    from services.document_service import DocumentService
    with mock_aws():
        _build_table()
        _build_bucket()
        sfn = MagicMock()
        sfn.start_execution.side_effect = RuntimeError('SF down')
        service = DocumentService(
            table_name=_TABLE,
            bucket=_BUCKET,
            region='us-east-1',
            state_machine_arn='arn:aws:states:us-east-1:0:stateMachine/x',
            sfn_client=sfn,
        )
        item = service.upload_document(
            ontology_id='o', filename='spec.md', body=b'hi'
        )
        assert item.get('executionStarted') is False
        assert 'SF down' in item.get('executionError', '')


def test_upload_skips_state_machine_when_arn_unset():
    """No SM ARN → no states:StartExecution call. Backwards compatibility
    for environments that haven't deployed the doc-pipeline stack yet."""
    from unittest.mock import MagicMock
    from services.document_service import DocumentService
    with mock_aws():
        _build_table()
        _build_bucket()
        sfn = MagicMock()
        service = DocumentService(
            table_name=_TABLE,
            bucket=_BUCKET,
            region='us-east-1',
            state_machine_arn='',
            sfn_client=sfn,
        )
        service.upload_document(
            ontology_id='o', filename='spec.md', body=b'hi'
        )
        sfn.start_execution.assert_not_called()
