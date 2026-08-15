# ADR 0001: Three data classes
Status: Accepted

Enterprise relational history, evidence knowledge and agent state are logically distinct even when stored in one CockroachDB deployment. Large SAP tables are queried through bounded services and are never injected wholesale into model context.
