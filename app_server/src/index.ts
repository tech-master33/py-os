import { handleCatalogRequest, type CatalogEnv } from "./catalog";

export interface AppEnv extends CatalogEnv {
  PUBLISH_TOKEN: string;
  PUBLISH_PUBLIC_KEY: string;
}

declare global {
  namespace Cloudflare {
    interface Env extends AppEnv {}
  }
}

export default {
  fetch(request: Request, env: AppEnv): Promise<Response> {
    return handleCatalogRequest(request, env);
  },
} satisfies ExportedHandler<AppEnv>;
