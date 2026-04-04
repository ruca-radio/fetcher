// Paste this into Chrome DevTools Console on x.com
// Then do a search. It will capture the SearchTimeline API call details.

(function() {
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';

    if (url.includes('SearchTimeline') || url.includes('search')) {
      console.log('%c=== TWITTER API CALL CAPTURED ===', 'color: lime; font-size: 16px');
      console.log('URL:', url);

      // Parse the URL
      const parsed = new URL(url, 'https://x.com');
      console.log('Endpoint:', parsed.pathname);
      console.log('QueryId:', parsed.pathname.split('/')[4]);

      // Log headers
      const opts = args[1] || {};
      if (opts.headers) {
        const h = opts.headers instanceof Headers
          ? Object.fromEntries(opts.headers.entries())
          : opts.headers;
        console.log('Headers:', JSON.stringify(h, null, 2));
      }

      // Log params
      for (const [k, v] of parsed.searchParams) {
        try {
          console.log(`Param ${k}:`, JSON.parse(v));
        } catch {
          console.log(`Param ${k}:`, v);
        }
      }
    }

    const response = await origFetch.apply(this, args);

    if (url.includes('SearchTimeline')) {
      // Clone and log response
      const clone = response.clone();
      try {
        const data = await clone.json();
        const instructions = data?.data?.search_by_raw_query?.search_timeline?.timeline?.instructions || [];
        let entries = [];
        for (const inst of instructions) {
          entries = entries.concat(inst.entries || []);
        }
        console.log(`Response: ${entries.length} entries`);

        // Print compact summary
        const summary = {
          endpoint: parsed.pathname,
          queryId: parsed.pathname.split('/')[4],
          totalEntries: entries.length,
          firstFewTweets: entries.slice(0, 3).map(e => {
            const r = e?.content?.itemContent?.tweet_results?.result;
            return {
              id: r?.rest_id,
              text: r?.legacy?.full_text?.substring(0, 60),
              user: r?.core?.user_results?.result?.legacy?.screen_name,
            };
          }),
        };

        console.log('%cCOPY THIS:', 'color: yellow; font-size: 14px');
        console.log(JSON.stringify(summary, null, 2));
      } catch(e) {
        console.log('Could not parse response:', e);
      }
    }

    return response;
  };

  // Also intercept XMLHttpRequest
  const origXHR = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    if (url && (url.includes('SearchTimeline') || url.includes('/search/'))) {
      console.log('%c=== XHR CAPTURED ===', 'color: orange; font-size: 14px');
      console.log('Method:', method, 'URL:', url);
    }
    return origXHR.call(this, method, url, ...rest);
  };

  console.log('%c✓ Twitter API capture active. Now do a search on x.com!', 'color: lime; font-size: 14px');
  console.log('After searching, copy the output and paste it back.');
})();
