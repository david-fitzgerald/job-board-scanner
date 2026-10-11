import unittest

from jobscan.config import Profile
from jobscan.fetch import Posting
from jobscan.prefilter import matches

PROFILE = Profile(
    name="ae", titles=("Account Executive", "Founding Sales"), exclude_titles=("Sales Development", "Junior", "intern"),
    locations=("London", "UK", "Remote (UK)"), eligible_where="the UK", tags=("founding",),
)


def posting(title, location="London"):
    return Posting(key="ashby:acme:1", board="acme", company="Acme", title=title, location=location,
                   url="https://jobs.ashbyhq.com/acme/1", description="")


class PrefilterTest(unittest.TestCase):
    def test_titles_kept(self):
        for title in ["Account Executive", "Senior Account Executive, Enterprise - UK", "account executive (UK)",
                      "Founding Sales (Munich Office)", "International Account Executive"]:
            self.assertTrue(matches(posting(title), PROFILE), title)

    def test_near_misses_and_excludes_dropped(self):
        for title in ["Senior Product Marketing Manager", "Technical Project Manager - UK", "Account Manager",
                      "Founding Sales Development Representative", "Inside Sales (Junior Account Executive)",
                      "Account Executives Lead", "Account Executive Intern"]:
            self.assertFalse(matches(posting(title), PROFILE), title)

    def test_locations_are_word_adjacent(self):
        self.assertTrue(matches(posting("Account Executive", "UK & Ireland"), PROFILE))
        self.assertTrue(matches(posting("Account Executive", "remote (uk)"), PROFILE))
        self.assertTrue(matches(posting("Account Executive", "Berlin / London"), PROFILE))
        self.assertFalse(matches(posting("Account Executive", "Kyiv, Ukraine"), PROFILE))
        self.assertFalse(matches(posting("Account Executive", "Londonderry"),
                                 PROFILE))
        self.assertFalse(matches(posting("Account Executive", ""), PROFILE))


if __name__ == "__main__":
    unittest.main()
