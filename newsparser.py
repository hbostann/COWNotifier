import html
from html.parser import HTMLParser
import os
import datetime
import traceback
import re
from emoji_codepoints import emoji


def isPlusOne(msg):
  return msg.startswith("+1") and len(msg) < 10


def getHumanReadableDate(date):
  # Date is expected to be a tuple of two elements, from newsreader.
  # date[0] -> message's date in raw form: %Y:%m:%dT%H:%M:%S.xxZ
  # where 'xx's and Z represent nanosecs(?) and the UTC +0 respectively.
  # date[1] -> list of timezone difference to localzone, Turkey's is UTC 3.00,
  #            so it is [3, 0] representing hours and minutes by default.
  rawDate, localTimezone = date
  # Parse the raw format and add localTimezone's hour difference.

  machineDate = datetime.datetime.strptime(
      rawDate.split('.')[0],
      "%Y-%m-%dT%H:%M:%S") + datetime.timedelta(hours=localTimezone)
  # return the human readable from, refer to datetime.strftime for format
  # options.
  # an example of current format: 03 Feb 2020, 18:30:00
  return machineDate.strftime("%d %b %Y, %H:%M:%S")


class articleParser(HTMLParser):
  # articleParser parses the raw html from Discourse and generates html that
  # Telegram likes. During the process it performs the following.
  #
  # Convert Tags: Telegram supports a very small subset of html tags. In order
  #               to provide (some) formatting in the telegram message, parser
  #               converts tags that telegram doesn't like into ones it does
  #               or removes them (not the contents) if no conversion exists.
  # Handle Image: Images are wrapped in <a> tags with href same as img's src.
  #               (<a><img></a>) In this case parser removes the <img> tag
  #               completely since Telegram handles <a> tags pretty well with
  #               previews. (Also telegram doesn't support <img> tags)
  # Handle Emoji: Emojis are represented as <img> tags but they are not wrapped
  #               in <a> tags, unlike the previous case. Parser replaces <img>
  #               tags with 'class="emoji"' with their unicode codepoint.

  # Tags supported by Telegram
  supported_tags = [
      'strike', 'i', 'strong', 'b', 'pre', 'ins', 'a', 'code', 'del', 's', 'em',
      'u'
  ]
  # Useful attributes to look for
  useful_attrs = ['href', 'title', 'class']
  # html_tag -> telegram_tag mapping for formatting purposes
  tag_mapping = {'h1': 'b', 'h2': 'b', 'h3': 'b', 'blockquote': 'i'}

  def __init__(self):
    super().__init__(convert_charrefs=False)
    self.reset()
    self.fed = []

  def handle_entityref(self, name):
    # This is done so that html entities like '&gt;' remain escaped.
    self.fed.append(f'&{name};')

  def handle_starttag(self, tag, attrs):
    tag_attrs = {at[0]: at[1] for at in attrs if at[0] in self.useful_attrs}
    ptag = tag
    if ptag not in self.supported_tags:
      # If tag is not supported, convert it
      ptag = self.tag_mapping.get(ptag, None)
    if ptag != None:
      # If tag is supported or converted add it to the message
      attr_str = ' href=\"' + tag_attrs.get('href',
                                            '') + '\"' if tag == 'a' else ''
      self.fed.append(f'<{ptag+attr_str}>')
    # Look for emojis
    if tag == 'img' and tag_attrs.get('class', '') == 'emoji':
      self.fed.append(emoji.get(tag_attrs.get('title', ':cow:'), ''))
      return

  def handle_data(self, data):
    self.fed.append(data)

  def handle_endtag(self, tag):
    # If tag is not supported/converted we don't need it's closing tag since
    # it is never opened.
    if tag not in self.supported_tags:
      tag = self.tag_mapping.get(tag, None)
    if tag != None:
      self.fed.append(f'</{tag}>')

  def get_data(self):
    return "".join(self.fed)


class newsArticle:

  def __init__(self, author, topic, subject, date, raw_msg, raw_html,
               mention_manager):
    self.author_username = author[0]
    self.author_displayname = author[1]
    self.topic = topic
    self.subject = subject
    self.date = getHumanReadableDate(date)
    self.raw_msg = raw_msg
    self.raw_html = raw_html
    self.mention_manager = mention_manager
    self.broken = False
    self.content = None
    self.is_plus_one = None

  def isPlusOne(self):
    if self.is_plus_one is None:
      self.is_plus_one = isPlusOne(self.raw_msg)
    return self.is_plus_one

  def getAsHtml(self):
    if self.content is None:
      self.parseMessage()
    return self.content

  # TODO: getAttachemnts:
  #  Extract the images and files in the message

  def makeHeader(self):
    hdr = f"From: {self.author_username}({self.author_displayname})\r\n"\
        f"Newsgroup: {self.topic}\r\n"\
        f"Subject: {self.subject}\r\n"\
        f"Date: {self.date}\r\n"\
        f"is_plus_one: {self.isPlusOne()}"
    return "<code>" + html.escape(hdr) + "</code>\n\n"

  def parseLinks(self, msg):
    # In markdown, discourse uses the following format for images and image links
    #     ![image_name](upload://<file_name_hash>)
    # However, in the html version of the message image links are regular http(s)
    # links. This part of the code removes the upload:// urls from the markdown and
    # replaces them with the image links from the html version.
    regex = r"!\[([^\]]*)\]\(([^\)]*)\)"
    img_tag = '<img src=\"'
    pattern = re.compile(regex)
    parsed = ""
    link_counter = 1
    last_pos = 0
    for m in pattern.finditer(msg):
      # Find link name
      name_start, name_end = m.span(1)
      link_name = msg[name_start:name_end]
      if link_name == "":
        link_name = f"Link {link_counter}"

      # Find first img link in html
      html_link_start = self.raw_html.find(img_tag) + len(img_tag)
      html_link_len = self.raw_html[html_link_start:].find('"')
      html_link = self.raw_html[html_link_start:html_link_start + html_link_len]
      # Discard used html
      self.raw_html = self.raw_html[html_link_start + html_link_len:]

      # Replace link in markdown
      match_start, match_end = m.span()
      parsed += msg[last_pos:match_start] + f"[{link_name}]({html_link})"

      last_pos = match_end
      link_counter += 1

    parsed += msg[last_pos:]
    return parsed

  def parseMessage(self):
    if self.broken:
      return

    try:
      hdr = self.makeHeader()
      p = articleParser()
      p.feed(self.raw_html)
      content = p.get_data()
      # TODO: Parse Markup
      # content = self.parseLinks(content)
      # content = ''.join(['\\'+c for c in content if ord(c) > 0 and ord(c) < 128])
      self.mention_manager.parseMentions(content, self.topic)
      self.content = hdr + content
    except Exception as e:
      print(e, datetime.datetime.now())
      traceback.print_exc()
      self.broken = True
