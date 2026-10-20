import { posix } from "node:path";

/** Follow an evidenced artifact only when the service presents a directory. */
export async function followDirectoryArtifact(page, artifactPath) {
  if (!await page.getByRole("heading", { name: /^Directory listing for / }).isVisible()) return false;
  if (!artifactPath?.startsWith("/") || !artifactPath.toLowerCase().endsWith(".html")) {
    throw new Error("Directory preview requires an evidenced absolute HTML path");
  }
  const name = posix.basename(artifactPath);
  const link = page.getByRole("link", { name, exact: true });
  if (await link.count() !== 1) throw new Error("Directory preview has no unique link to the evidenced artifact");
  const current = new URL(page.url());
  const target = new URL(await link.getAttribute("href"), current);
  if (target.origin !== current.origin || target.username || target.password
      || target.search || target.hash || decodeURIComponent(posix.basename(target.pathname)) !== name) {
    throw new Error("Directory artifact link leaves the trusted preview or names another file");
  }
  await link.click();
  return true;
}
