# Vercel Python serverless function.
# Route: POST /api/render-coffee-and-tea-pdf
# Body: { "output": <ReasoningOutput JSON from /api/coffee-and-tea>,
#          "session_date": "2026-09-15", "expiration": "2026-09-18",
#          "portfolio_size": 30000, "spot": 764.48 }
# Returns: raw PDF bytes (Content-Type: application/pdf), not JSON.
#
# This is the styled render layer from the other chat's project, wired in
# as-is (render_pdf_lib.py + schema.py are unmodified copies) rather than
# reimplemented in jsPDF, to preserve the actual designed layout (colors,
# Appendix A/B/C/D structure, table styling) instead of the simpler
# approximation the client-side jsPDF version produced.
#
# Validates the output through the real pydantic ReasoningOutput schema
# before rendering - a second, more rigorous validation layer on top of
# the lighter JS checks already done in coffee-and-tea.js. A response
# that failed this validation would mean something is wrong with the
# reasoning layer's output shape, not just a rendering hiccup.

from http.server import BaseHTTPRequestHandler
import json
import os
import tempfile

from pydantic import ValidationError
from schema import ReasoningOutput
from render_pdf_lib import generate_pdf


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body_raw = self.rfile.read(content_length)
            body = json.loads(body_raw)

            output_data = body.get('output')
            session_date = body.get('session_date')
            expiration = body.get('expiration')
            portfolio_size = body.get('portfolio_size')
            spot = body.get('spot')

            if not all([output_data, session_date, expiration, portfolio_size, spot]):
                self._send_json_error(400, 'Missing one of: output, session_date, expiration, portfolio_size, spot')
                return

            try:
                output = ReasoningOutput.model_validate(output_data)
            except ValidationError as e:
                self._send_json_error(422, f'Output failed schema validation: {e}')
                return

            tmp_dir = tempfile.mkdtemp()
            out_path = os.path.join(tmp_dir, 'session.pdf')

            generate_pdf(
                output,
                session_date=session_date,
                expiration=expiration,
                portfolio_size=float(portfolio_size),
                spot=float(spot),
                out_path=out_path,
            )

            with open(out_path, 'rb') as f:
                pdf_bytes = f.read()

            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', f'attachment; filename="Coffee_and_Tea_{session_date}.pdf"')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(pdf_bytes)

        except Exception as e:
            self._send_json_error(500, f'Server error: {e}')

    def _send_json_error(self, status, message):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}).encode())
