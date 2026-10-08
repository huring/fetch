# Backlog

## Change how manual searches are triggered
Currently i can trigger a manual search per marketplace, that then performs all searches that are connected to that marketplace. I want to change how that works:

I want to be able to manually trigger a specific search (both plain and rated), that would then perform only that specific search on the selected marketplaces. I don't want to trigger the searches from the marketplace tabs

## User interface and UX
We need to make improvements to the user interface, and use a more modern, nice looking style for both the start page, the listings and mainly for the search creation page. We should start by implementing Tailwind css and a theme that we can continue to work with in the future.

### Guidelines:
- Only show what is relevant: don't show form fields that aren't relevant for my current context, so if i'm creating a plain search, i don't need to see the claude/rating specific fields
- Responsive, i would like to be able to use this on my phone most of the time, so all pages needs to be responsive and look good both on my desktop and on my phone
- Use a dark theme, no need for a light theme
- Better use of labels and indicators to make the UI nicer to look at

## Setting per search if it should be included in summary, and in what way
I want a setting per search how i should be notified. For some searches it's enough to provide a link to our listing. This should be the default setting for all searches that are not rated, to avoid spamming the slack channel with lots of items 

### Example
- Looking for vinyls that will result in lots of hits, in the summary i only want a link that says "N new items in your <search name>", not every new item in the slack message.
- Looking for a very specific type of amplifier below a certain price, i want to be notified right away when that amp is found with the specified accessories at my price. 