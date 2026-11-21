// Starter scenario: two players in two browser contexts. Adapt the selectors to the game; keep the shape —
// every acceptance criterion of a game task is a test like this, and the tester role runs them with `team-play test`.
// A spec that proves an acceptance criterion carries `@live` in its title (the tester runs `team-play test --grep @live`
// against the running game); only a spec that needs a local-only hook goes without the tag and is named in the task's limits note.
import { test, expect, type Page } from "@playwright/test";

async function createRoom(page: Page): Promise<string> {
  await page.goto("/");
  await page.getByRole("button", { name: /create room/i }).click();
  const code = await page.getByTestId("room-code").textContent();
  expect(code, "the lobby shows a room code").toMatch(/^[A-Z0-9]{4,8}$/);
  return code!.trim();
}

test("two tabs join one room and see each other's move within 1 s @live", async ({ browser }) => {
  const a = await (await browser.newContext()).newPage();
  const b = await (await browser.newContext()).newPage();
  const code = await createRoom(a);

  await b.goto("/");
  await b.getByLabel(/room code/i).fill(code);
  await b.getByRole("button", { name: /join/i }).click();

  await expect(a.getByTestId("status")).toHaveText(/your turn/i, { timeout: 5_000 });
  await expect(b.getByTestId("status")).toHaveText(/opponent's turn/i);

  const t0 = Date.now();
  await a.getByTestId("cell-0-0").click();           // the first legal move of the game
  await expect(b.getByTestId("cell-0-0")).toHaveAttribute("data-owner", "A", { timeout: 1_000 });
  expect(Date.now() - t0, "the move reaches the opponent within 1 s").toBeLessThan(1_000);
});

test("an illegal move is rejected by the server and the board does not change @live", async ({ browser }) => {
  const a = await (await browser.newContext()).newPage();
  const b = await (await browser.newContext()).newPage();
  const code = await createRoom(a);
  await b.goto("/");
  await b.getByLabel(/room code/i).fill(code);
  await b.getByRole("button", { name: /join/i }).click();

  await expect(b.getByTestId("status")).toHaveText(/opponent's turn/i);
  await b.getByTestId("cell-0-0").click();            // not B's turn
  await expect(b.getByTestId("error")).toHaveText(/not your turn/i);
  await expect(a.getByTestId("cell-0-0")).not.toHaveAttribute("data-owner", "B");
});

test("a third player gets a clear refusal @live", async ({ browser }) => {
  const a = await (await browser.newContext()).newPage();
  const code = await createRoom(a);
  for (const who of ["b", "c"]) {
    const p = await (await browser.newContext()).newPage();
    await p.goto("/");
    await p.getByLabel(/room code/i).fill(code);
    await p.getByRole("button", { name: /join/i }).click();
    if (who === "c") await expect(p.getByTestId("error")).toHaveText(/room is full/i);
  }
});
