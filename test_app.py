import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from app import BrowseScreen, DatabasePicker, RolodexApp, RunScreen


class FakeStore:
    def __init__(self):
        self.searches = []

    def search(self, **filters):
        self.searches.append(filters)
        return [], 0

    def distinct_folders(self):
        return []


class TestBrowseScreen(BrowseScreen):
    def load_db(self):
        self.store = FakeStore()
        self.run_search()


class IdleRunScreen(RunScreen):
    def on_mount(self):
        pass

    def run_extraction(self):
        pass


class LiveSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_updates_after_typing(self):
        app = RolodexApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = TestBrowseScreen()
            app.push_screen(screen)
            await pilot.pause()

            screen.query_one("#query", Input).value = "alice"
            await pilot.pause(0.3)

            self.assertEqual(screen.store.searches[-1]["search"], "alice")

    async def test_database_picker_starts_in_working_directory(self):
        app = RolodexApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = TestBrowseScreen()
            app.push_screen(screen)
            await pilot.pause()

            await pilot.click("#choose_db")

            self.assertIsInstance(app.screen, DatabasePicker)
            self.assertEqual(app.screen.directory, Path.cwd())

    async def test_run_log_can_be_copied_on_macos(self):
        app = RolodexApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = IdleRunScreen(None, [], [], "test.db", "host", "user")
            screen.log_lines = ["first", "second"]
            app.push_screen(screen)
            await pilot.pause()

            with patch("app.sys.platform", "darwin"), patch("app.subprocess.run") as run:
                await pilot.click("#copy_log")

            run.assert_called_once_with(
                ["pbcopy"], input="first\nsecond", text=True, check=True
            )


if __name__ == "__main__":
    unittest.main()
