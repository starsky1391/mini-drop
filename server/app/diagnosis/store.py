"""诊断控制层持久化访问。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from server.app.database import new_session
from server.app.models import (
    DiagnosisEventModel,
    DiagnosisEvidenceModel,
    DiagnosisSessionModel,
    ProbeExecutionModel,
    TopologySnapshotModel,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DiagnosisStore:
    def create_topology_snapshot(self, snapshot: dict[str, Any]) -> None:
        session = new_session()
        try:
            session.add(TopologySnapshotModel(
                id=snapshot["snapshot_id"],
                effective_at=snapshot["effective_at"],
                generated_at=snapshot["generated_at"],
                nodes_json=snapshot.get("nodes", []),
                edges_json=snapshot.get("edges", []),
                source_versions_json=snapshot.get("source_versions", {}),
                confidence_summary_json=snapshot.get("confidence_summary", {}),
            ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_session(self, data: dict[str, Any]) -> None:
        now = utcnow()
        session = new_session()
        try:
            model = DiagnosisSessionModel(
                id=data["diagnosis_id"],
                creator_id=data["creator_id"],
                raw_query=data["raw_query"],
                normalized_intent_json=data.get("normalized_intent", {}),
                target_scope_json=data.get("target_scope", {}),
                requested_time_range_json=data.get("requested_time_range", {}),
                effective_time_range_json=data.get("effective_time_range", {}),
                topology_snapshot_id=data.get("topology_snapshot_id"),
                baseline_snapshot_id=data.get("baseline_snapshot_id"),
                status=data["status"],
                policy_profile=data["policy_profile"],
                risk_budget_json=data.get("risk_budget", {}),
                resource_budget_json=data.get("resource_budget", {}),
                budget_used_json=data.get("budget_used", {}),
                hypothesis_graph_json=data.get("hypothesis_graph", {}),
                child_task_ids_json=data.get("child_task_ids", []),
                conclusion_versions_json=data.get("conclusion_versions", []),
                model_version=data["model_version"],
                planner_version=data["planner_version"],
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            session.flush()
            session.add(DiagnosisEventModel(
                diagnosis_id=model.id,
                event_type="diagnosis_created",
                from_status=None,
                to_status=model.status,
                payload_json={},
                created_at=now,
            ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_session(self, diagnosis_id: str) -> dict[str, Any] | None:
        session = new_session()
        try:
            model = session.get(DiagnosisSessionModel, diagnosis_id)
            return model.to_dict() if model else None
        finally:
            session.close()

    def list_sessions(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = new_session()
        try:
            rows = (
                session.query(DiagnosisSessionModel)
                .order_by(DiagnosisSessionModel.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [row.to_dict() for row in rows]
        finally:
            session.close()

    def count_sessions(self) -> int:
        session = new_session()
        try:
            return session.query(DiagnosisSessionModel).count()
        finally:
            session.close()

    def update_session(self, diagnosis_id: str, **fields: Any) -> dict[str, Any]:
        column_map = {
            "normalized_intent": "normalized_intent_json",
            "target_scope": "target_scope_json",
            "effective_time_range": "effective_time_range_json",
            "budget_used": "budget_used_json",
            "hypothesis_graph": "hypothesis_graph_json",
            "child_task_ids": "child_task_ids_json",
            "conclusion_versions": "conclusion_versions_json",
            "status": "status",
            "lease_owner": "lease_owner",
            "lease_until": "lease_until",
        }
        unknown = set(fields) - set(column_map)
        if unknown:
            raise ValueError(f"不允许更新诊断字段: {sorted(unknown)}")
        session = new_session()
        try:
            model = session.get(DiagnosisSessionModel, diagnosis_id)
            if model is None:
                raise ValueError(f"诊断 {diagnosis_id} 不存在")
            for key, value in fields.items():
                setattr(model, column_map[key], value)
            model.updated_at = utcnow()
            session.commit()
            return model.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def transition(
        self,
        diagnosis_id: str,
        to_status: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = new_session()
        try:
            model = session.get(DiagnosisSessionModel, diagnosis_id)
            if model is None:
                raise ValueError(f"诊断 {diagnosis_id} 不存在")
            previous = model.status
            model.status = to_status
            model.updated_at = utcnow()
            session.add(DiagnosisEventModel(
                diagnosis_id=diagnosis_id,
                event_type=event_type,
                from_status=previous,
                to_status=to_status,
                payload_json=payload or {},
                created_at=utcnow(),
            ))
            session.commit()
            return model.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_event(
        self,
        diagnosis_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        session = new_session()
        try:
            model = session.get(DiagnosisSessionModel, diagnosis_id)
            if model is None:
                raise ValueError(f"诊断 {diagnosis_id} 不存在")
            session.add(DiagnosisEventModel(
                diagnosis_id=diagnosis_id,
                event_type=event_type,
                from_status=model.status,
                to_status=model.status,
                payload_json=payload or {},
                created_at=utcnow(),
            ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def acquire_lease(self, diagnosis_id: str, owner: str, ttl_seconds: int = 30) -> bool:
        """短租约避免多个 API 实例同时推进同一会话。"""
        now = utcnow()
        session = new_session()
        try:
            model = (
                session.query(DiagnosisSessionModel)
                .filter(DiagnosisSessionModel.id == diagnosis_id)
                .with_for_update()
                .first()
            )
            if model is None:
                return False
            lease_until = model.lease_until
            if lease_until is not None and lease_until.tzinfo is None:
                lease_until = lease_until.replace(tzinfo=timezone.utc)
            if lease_until and lease_until > now and model.lease_owner != owner:
                return False
            model.lease_owner = owner
            model.lease_until = now + timedelta(seconds=ttl_seconds)
            model.updated_at = now
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def release_lease(self, diagnosis_id: str, owner: str) -> None:
        session = new_session()
        try:
            model = session.get(DiagnosisSessionModel, diagnosis_id)
            if model is not None and model.lease_owner == owner:
                model.lease_owner = None
                model.lease_until = None
                model.updated_at = utcnow()
                session.commit()
        finally:
            session.close()

    def add_probe(self, probe: dict[str, Any]) -> dict[str, Any]:
        session = new_session()
        try:
            existing = session.get(ProbeExecutionModel, probe["step_id"])
            if existing is not None:
                return existing.to_dict()
            now = utcnow()
            model = ProbeExecutionModel(
                id=probe["step_id"],
                diagnosis_id=probe["diagnosis_id"],
                probe_id=probe["probe_id"],
                target_json=probe.get("target", {}),
                parameters_json=probe.get("parameters", {}),
                reason=probe["reason"],
                risk_level=probe["risk_level"],
                status=probe["status"],
                requires_approval=1 if probe.get("requires_approval") else 0,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            session.commit()
            return model.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_probe(self, step_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "task_id", "approved_by", "approved_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新探针字段: {sorted(unknown)}")
        session = new_session()
        try:
            model = session.get(ProbeExecutionModel, step_id)
            if model is None:
                raise ValueError(f"探针步骤 {step_id} 不存在")
            for key, value in fields.items():
                setattr(model, key, value)
            model.updated_at = utcnow()
            session.commit()
            return model.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_probe(self, step_id: str) -> dict[str, Any] | None:
        session = new_session()
        try:
            model = session.get(ProbeExecutionModel, step_id)
            return model.to_dict() if model else None
        finally:
            session.close()

    def list_probes(self, diagnosis_id: str) -> list[dict[str, Any]]:
        session = new_session()
        try:
            rows = (
                session.query(ProbeExecutionModel)
                .filter(ProbeExecutionModel.diagnosis_id == diagnosis_id)
                .order_by(ProbeExecutionModel.created_at.asc())
                .all()
            )
            return [row.to_dict() for row in rows]
        finally:
            session.close()

    def add_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        session = new_session()
        try:
            existing = session.get(DiagnosisEvidenceModel, evidence["evidence_id"])
            if existing is not None:
                return existing.to_dict()
            model = DiagnosisEvidenceModel(
                id=evidence["evidence_id"],
                diagnosis_id=evidence["diagnosis_id"],
                source_type=evidence["source_type"],
                source_system=evidence["source_system"],
                target_json=evidence.get("target", {}),
                event_time_range_json=evidence.get("event_time_range", {}),
                ingestion_time=evidence.get("ingestion_time", utcnow()),
                query_or_probe=evidence["query_or_probe"],
                raw_artifact_ref=evidence.get("raw_artifact_ref"),
                derived_artifact_ref=evidence.get("derived_artifact_ref"),
                derivation_version=evidence.get("derivation_version", "v1"),
                evidence_index_json=evidence.get("evidence_index", {}),
                observed_value_json=evidence.get("observed_value", {}),
                baseline_value_json=evidence.get("baseline_value", {}),
                anomaly_score_json=evidence.get("anomaly_score", {}),
                data_quality_json=evidence.get("data_quality", {}),
                integrity_hash=evidence["integrity_hash"],
                claim_links_json=evidence.get("claim_links", []),
            )
            session.add(model)
            session.commit()
            return model.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        session = new_session()
        try:
            model = session.get(DiagnosisEvidenceModel, evidence_id)
            return model.to_dict() if model else None
        finally:
            session.close()

    def resolve_evidence_ref(self, diagnosis_id: str, evidence_ref: str) -> dict[str, Any] | None:
        for item in self.list_evidence(diagnosis_id):
            if item.get("evidence_id") == evidence_ref:
                return item
            index = item.get("evidence_index") or {}
            if _evidence_ref_in_index(evidence_ref, index):
                return item
        return None

    def list_evidence(self, diagnosis_id: str) -> list[dict[str, Any]]:
        session = new_session()
        try:
            rows = (
                session.query(DiagnosisEvidenceModel)
                .filter(DiagnosisEvidenceModel.diagnosis_id == diagnosis_id)
                .order_by(DiagnosisEvidenceModel.ingestion_time.asc())
                .all()
            )
            return [row.to_dict() for row in rows]
        finally:
            session.close()

    def get_topology(self, snapshot_id: str | None) -> dict[str, Any] | None:
        if not snapshot_id:
            return None
        session = new_session()
        try:
            model = session.get(TopologySnapshotModel, snapshot_id)
            return model.to_dict() if model else None
        finally:
            session.close()

    def get_detail(self, diagnosis_id: str) -> dict[str, Any] | None:
        item = self.get_session(diagnosis_id)
        if item is None:
            return None
        session = new_session()
        try:
            events = (
                session.query(DiagnosisEventModel)
                .filter(DiagnosisEventModel.diagnosis_id == diagnosis_id)
                .order_by(DiagnosisEventModel.id.asc())
                .all()
            )
            item["events"] = [event.to_dict() for event in events]
        finally:
            session.close()
        item["topology_snapshot"] = self.get_topology(item.get("topology_snapshot_id"))
        item["probes"] = self.list_probes(diagnosis_id)
        item["evidence"] = self.list_evidence(diagnosis_id)
        conclusions = item.get("conclusion_versions", [])
        item["latest_conclusion"] = conclusions[-1] if conclusions else None
        return item


def _evidence_ref_in_index(evidence_ref: str, index: dict[str, Any]) -> bool:
    if not isinstance(index, dict):
        return False
    normalized_ref = evidence_ref
    if normalized_ref.startswith("evidence_index."):
        normalized_ref = normalized_ref[len("evidence_index."):]
    normalized_ref = normalized_ref.replace("[", ".").replace("]", "")
    normalized_ref = ".".join(part for part in normalized_ref.split(".") if part and not part.isdigit())
    normalized_paths = _collect_index_paths(index)
    return normalized_ref in normalized_paths or evidence_ref in normalized_paths


def _collect_index_paths(value: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_prefix = f"{prefix}.{key_text}" if prefix else key_text
            paths.add(next_prefix)
            paths.update(_collect_index_paths(item, next_prefix))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.update(_collect_index_paths(item, prefix))
        return paths
    if prefix:
        paths.add(prefix)
    return paths
