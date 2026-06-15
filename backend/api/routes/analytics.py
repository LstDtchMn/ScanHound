"""Analytics endpoints: library stats, scan trends, quality breakdown."""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.analytics import StatsDashboard

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _get_dashboard(reg: ServiceRegistry) -> StatsDashboard:
    dashboard = reg.analytics
    if dashboard is None:
        raise HTTPException(status_code=503, detail="Analytics service unavailable")
    return dashboard


@router.get("/summary")
def dashboard_summary(reg: ServiceRegistry = Depends(get_registry)):
    """Full dashboard summary (library + scans + trends + quality)."""
    return _get_dashboard(reg).get_dashboard_summary()


@router.get("/library")
def library_stats(
    mode: str = Query("Movies", pattern="^(Movies|TV Shows)$"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Library stats for a content type."""
    return _get_dashboard(reg).get_library_stats(mode).to_dict()


@router.get("/scans")
def scan_stats(
    days: int = Query(30, ge=1, le=365),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Scan history statistics."""
    return _get_dashboard(reg).get_scan_stats(days).to_dict()


@router.get("/trends")
def trend_data(
    days: int = Query(30, ge=1, le=365),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Trend data for charts."""
    return _get_dashboard(reg).get_trend_data(days)


@router.get("/quality")
def quality_breakdown(
    mode: str = Query("Movies", pattern="^(Movies|TV Shows)$"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Detailed quality breakdown (resolution + HDR distribution)."""
    return _get_dashboard(reg).get_quality_breakdown(mode)


@router.get("/scan-history")
def scan_history_list(
    limit: int = Query(20, ge=1, le=100),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Return recent individual scan records."""
    if not reg.db:
        return []
    try:
        conn = reg.db.get_connection()
        if not conn:
            return []
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM scan_history ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception:
        return []


@router.get("/export")
def export_report(
    format: str = Query("json", pattern="^(json|html)$"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Export analytics report."""
    from fastapi.responses import HTMLResponse, JSONResponse
    dashboard = _get_dashboard(reg)
    if format == "html":
        return HTMLResponse(content=dashboard.export_report("html"))
    return JSONResponse(content=dashboard.get_dashboard_summary())
