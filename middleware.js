// Vercel Edge Middleware - runs before every request to this site.
// This must live at the REPO ROOT (not inside /api) as `middleware.js` for
// Vercel to pick it up automatically.
//
// Implements simple HTTP Basic Auth as a free-tier workaround for
// Password Protection, which Vercel only offers natively on Pro/Enterprise.
// The browser will show a native username/password prompt before allowing
// access to any page or API route on this deployment.
//
// SETUP: after adding this file, go to your Vercel project's Settings ->
// Environment Variables and add two new variables:
//   SITE_USERNAME  -> whatever username you want
//   SITE_PASSWORD  -> whatever password you want
// Then redeploy. Anyone visiting the site will be prompted for these
// before seeing anything.

export const config = {
  matcher: '/((?!_next/static|_next/image|favicon.ico).*)',
};

export default function middleware(request) {
  const authHeader = request.headers.get('authorization');

  const expectedUser = process.env.SITE_USERNAME;
  const expectedPass = process.env.SITE_PASSWORD;

  if (authHeader) {
    const base64Credentials = authHeader.split(' ')[1] || '';
    const decoded = atob(base64Credentials);
    const [user, pass] = decoded.split(':');

    if (user === expectedUser && pass === expectedPass) {
      return; // credentials correct - let the request through
    }
  }

  return new Response('Authentication required', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="GEX Visualizer"',
    },
  });
}
