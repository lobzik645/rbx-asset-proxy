/**
 * Roblox Asset Proxy - Cloudflare Worker Template (Опционально)
 * 
 * Если у вашего провайдера полностью заблокированы IP-адреса CDN серверов Roblox,
 * этот воркер можно бесплатно развернуть в Cloudflare Workers и использовать
 * как удалённый прокси-эндпоинт.
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Домен назначения передается в заголовке X-Target-Host или в query параметре ?host=
    const targetHost = request.headers.get("X-Target-Host") || url.searchParams.get("host") || "tr.rbxcdn.com";
    
    // Формируем целевой URL к Roblox CDN
    const targetUrl = new URL(request.url);
    targetUrl.hostname = targetHost;
    targetUrl.protocol = "https:";
    targetUrl.port = "443";
    targetUrl.searchParams.delete("host");

    const newHeaders = new Headers(request.headers);
    newHeaders.set("Host", targetHost);
    newHeaders.set("User-Agent", "Roblox/WinInet");

    try {
      const response = await fetch(targetUrl.toString(), {
        method: request.method,
        headers: newHeaders,
        body: (request.method !== "GET" && request.method !== "HEAD") ? request.body : undefined,
        redirect: "follow"
      });

      // Пробрасываем ответ обратно локальному клиенту
      const responseHeaders = new Headers(response.headers);
      responseHeaders.set("Access-Control-Allow-Origin", "*");
      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders
      });
    } catch (err) {
      return new Response("Worker Error: " + err.message, { status: 502 });
    }
  }
};
