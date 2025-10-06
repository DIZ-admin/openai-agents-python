"""ValidationReportAgent - Generate validation reports and quality metrics."""

import time

from agents import Agent, function_tool

from ..events import EventEmitter


@function_tool
async def generate_simple_validation_report(
    batch_id: str, total_processed: int = 0, successful_uploads: int = 0
) -> str:
    """
    Generate simple validation report for processed batch

    Args:
        batch_id: Unique batch identifier
        total_processed: Total images processed
        successful_uploads: Number of successful uploads

    Returns:
        Simple validation report as string
    """
    emitter = EventEmitter(component="validation_report", batch_id=batch_id)

    try:
        # Calculate basic metrics
        success_rate = successful_uploads / total_processed if total_processed > 0 else 0.0

        emitter.emit_started(
            "Validation report generation",
            context={
                "total_processed": total_processed,
                "successful_uploads": successful_uploads,
                "success_rate": success_rate,
            }
        )

        # Generate simple report
        report = f"""
VALIDATION REPORT
================
Batch ID: {batch_id}
Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}

SUMMARY:
- Total Processed: {total_processed}
- Successful Uploads: {successful_uploads}
- Success Rate: {success_rate:.2%}
- Status: {"PASSED" if success_rate >= 0.9 else "FAILED"}

RECOMMENDATIONS:
{"- System performing well" if success_rate >= 0.9 else "- Review processing parameters and image quality"}
        """

        emitter.emit_completed(
            "Validation report generation",
            context={
                "report_length": len(report),
                "status": "PASSED" if success_rate >= 0.9 else "FAILED"
            }
        )

        return report.strip()

    except Exception as e:
        emitter.emit_failed("Validation report generation", error=e)
        raise


class ValidationReportAgent(Agent):
    """Agent for generating validation reports and quality metrics"""

    def __init__(self) -> None:
        super().__init__(
            name="ValidationReport",
            instructions=(
                "You are an expert in quality assurance and validation reporting.\n\n"
                "Your responsibilities:\n"
                "1. Generate comprehensive validation reports for processed image batches\n"
                "2. Calculate quality metrics (success rate, confidence, accuracy, cost)\n"
                "3. Check SLO compliance against targets (p95 ≤ 45s, success ≥ 0.94, cost ≤ $0.055)\n"
                "4. Analyze field validation accuracy for 23 SharePoint fields\n"
                "5. Monitor PII detection effectiveness and blocking rates\n"
                "6. Provide actionable recommendations for system improvement\n\n"
                "Always provide detailed analysis with specific metrics and clear recommendations.\n"
                "Focus on identifying patterns and root causes of quality issues.\n\n"
                "Quality targets:\n"
                "- Success rate ≥ 0.94\n"
                "- Choice field accuracy ≥ 0.90\n"
                "- PII detection FN-rate ≤ 1.0%\n"
                "- Field coverage ≥ 0.60\n"
                "- Cost efficiency ≥ 1.0 (under target)"
            ),
            tools=[generate_simple_validation_report],
        )
